# SPDX-License-Identifier: LGPL-3.0-or-later
"""The board: what it is, what it can hold, and how its lines are wired."""

from __future__ import annotations

import asyncio

from statemachined._proto.statemachined.v1 import service_pb2_grpc
from statemachined.daemon.api import convert
from statemachined.daemon.api.rig_service import RigService
from statemachined.daemon.firmware_manifest import compare_firmware, installed_firmware_version
from statemachined.model.line_map import LineMap

from .observer_registration import watching
from .refusals import Category, Refusal, answering, no_board_attached, reading
from .state_servicer import WATCH_PERIOD_SECONDS


class DeviceServicer(service_pb2_grpc.DeviceServicer):
    def __init__(self, service: RigService) -> None:
        self.service = service

    def _device_state(self):
        supervisor = self.service.supervisor
        return convert.device_state_to_wire(
            connected=supervisor.is_connected,
            target=self.service.configuration.device_target,
            hello_ack=supervisor.hello_ack,
            state_report=self.service.read_device_state(),
            capabilities=supervisor.capabilities,
            committed=supervisor.committed_graph_set,
            pin_labels_came_from=supervisor.pin_map.source,
            connection_count=supervisor.connection_count,
            last_error=self.service.last_error_from_the_device or "",
        )

    async def ReadDevice(self, request, context):
        return await answering(context, lambda: self._device_state())

    async def OpenLink(self, request, context):
        def body():
            self.service.connect()
            return self._device_state()

        return await answering(context, body)

    def _line_map_view(self):
        supervisor = self.service.supervisor
        report = self.service.read_device_state()
        io = report.get("io") or {}
        # The *resolved* map where there is a board to resolve against, so a
        # line configured by pin reports the index it will be uploaded with.
        line_map: LineMap = (
            supervisor.resolved_line_map if supervisor.is_connected else supervisor.line_map
        )
        return convert.line_map_view_to_wire(
            line_map,
            input_word=io.get("in"),
            output_word=io.get("out"),
            pin_labels_came_from=supervisor.pin_map.source,
            board_input_pins=list(supervisor.pin_map.input_pin_labels),
            board_output_pins=list(supervisor.pin_map.output_pin_labels),
        )

    async def ReadLines(self, request, context):
        return await answering(context, lambda: self._line_map_view())

    async def WriteLineMapFile(self, request, context):
        """Replace the line map, from its text.

        **A line map is a document** (`contracts/DAEMON_LAYOUT.md` §1), so what
        crosses is the file and the model that owns it is the only thing that
        decides whether it is one. A map that names two lines the same, or a
        line that says neither which index it is nor which pin, is refused by
        field name here rather than uploaded and puzzled over on a bench.
        """

        def body():
            try:
                line_map = LineMap.model_validate_json(request.text)
            except ValueError as problem:
                raise Refusal(
                    Category.BAD_REQUEST, "bad_line_map", str(problem), "text"
                ) from problem
            try:
                _resolved, pushed = self.service.apply_line_map(line_map)
            except ValueError as problem:
                # A map naming a pin this board does not have. Refused with the
                # rig still running on the map it had.
                raise Refusal(
                    Category.BAD_REQUEST,
                    "line_map_does_not_match_the_board",
                    str(problem),
                    "line_map",
                ) from problem
            config = self.service.state_machine_config
            return convert.write_line_map_result_to_wire(
                self._line_map_view(),
                pushed_to_device=pushed,
                state_machine_config=config.name if config is not None else "",
            )

        return await answering(context, body)

    async def ReadSerialMonitor(self, request, context):
        def body():
            monitor = self.service.line_monitor
            oldest = monitor.oldest_entry_number_still_held()
            return convert.serial_monitor_window_to_wire(
                monitor.lines_since(request.since_entry_number, limit=request.limit or 500),
                newest_entry_number=monitor.newest_entry_number(),
                oldest_entry_number_still_held=oldest,
                ring_capacity=monitor.ring_capacity,
                lost_entries_before=(
                    oldest
                    if monitor.has_fallen_out_of_the_ring(request.since_entry_number)
                    else None
                ),
            )

        return await answering(context, body)

    async def WatchSerialMonitor(self, request, context):
        monitor = self.service.line_monitor
        next_entry_number = request.since_entry_number
        with watching(self.service, context, stream="monitor") as observer_id:
            while True:
                # Bound explicitly: the lambda runs on another thread, and
                # `next_entry_number` is reassigned in this loop. It happens to
                # be awaited immediately, which is exactly the kind of "happens
                # to" that stops being true when somebody adds a line.
                entries = await reading(
                    lambda since=next_entry_number: monitor.lines_since(since, limit=500)
                )
                for entry in entries:
                    yield convert.serial_monitor_entry_to_wire(entry)
                    next_entry_number = entry["entry_number"] + 1
                self.service.observers.note_delivery(observer_id, len(entries))
                if not entries:
                    await asyncio.sleep(WATCH_PERIOD_SECONDS)

    async def ReadFirmware(self, request, context):
        """What the board runs against what this package ships.

        **The comparison is what matters**: a board in a rack cannot be asked
        which commit it is running, so the package carries a manifest and this
        says whether the two agree. Flashing is deliberately not here — it
        means dropping the port mid-session, which is a different risk from
        anything else this daemon does.
        """

        def body():
            running = (self.service.supervisor.hello_ack or {}).get("fw")
            if running is None:
                raise no_board_attached("no board is connected, so nothing is running")
            return convert.firmware_versions_to_wire(
                compare_firmware(running, installed_firmware_version())
            )

        return await answering(context, body)

    async def ReadAutorun(self, request, context):
        def body():
            if not self.service.supervisor.is_connected:
                raise no_board_attached("autorun is the board's own setting")
            return convert.autorun_to_wire(self.service.read_autorun())

        return await answering(context, body)

    async def WriteAutorun(self, request, context):
        def body():
            if not self.service.supervisor.is_connected:
                raise no_board_attached("autorun is the board's own setting")
            return convert.autorun_to_wire(
                self.service.set_autorun(
                    request.enabled, **convert.autorun_request_from_wire(request)
                )
            )

        return await answering(context, body)

    async def SaveSettings(self, request, context):
        """Write the wiring, the graph set and autorun to the board's flash.

        The answer is what the *board* said, not "it worked": `write_count` is
        a wear budget made visible, and `written: false` means the settings
        were already there and no erase cycle was spent. This returned an `Ok`
        in the first cut of the interface, which threw both away.
        """

        def body():
            if not self.service.supervisor.is_connected:
                raise no_board_attached()
            return convert.save_settings_result_to_wire(self.service.save_device_settings())

        return await answering(context, body)
