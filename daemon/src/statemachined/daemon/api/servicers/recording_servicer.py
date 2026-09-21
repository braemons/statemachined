# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writing the trace to disk.

The recorder is a sink on the trace, so nothing here decides what is recorded —
only whether it is being written, and where.
"""

from __future__ import annotations

from statemachined._proto.statemachined.v1 import service_pb2_grpc
from statemachined.daemon.api import convert
from statemachined.daemon.api.rig_service import RigService

from .refusals import answering


class RecordingServicer(service_pb2_grpc.RecordingServicer):
    def __init__(self, service: RigService) -> None:
        self.service = service

    def _recordings(self):
        recorder = self.service.recorder
        return convert.recordings_to_wire(recorder.manifests(), active=recorder.active)

    async def ReadRecordings(self, request, context):
        def body():
            return self._recordings()

        return await answering(context, body)

    async def Start(self, request, context):
        def body():
            # The service reads the loaded config itself — which config a
            # recording was made under is its decision, not a caller's, and a
            # servicer passing one in would be a second opinion about a rig
            # that already has one. An empty name means "let it choose", which
            # is what a person pressing Record means.
            return convert.manifest_to_wire(
                self.service.start_recording(
                    name=request.name or None, description=request.description
                )
            )

        return await answering(context, body)

    async def Pause(self, request, context):
        """Keep the file, stop writing to it.

        A pause is a real gap in the entry numbers and is recorded as a segment
        boundary rather than smoothed over: a reader that saw the jump without
        it would have to guess whether it was a pause or a loss.
        """

        def body():
            return convert.manifest_to_wire(self.service.pause_recording())

        return await answering(context, body)

    async def Resume(self, request, context):
        def body():
            return convert.manifest_to_wire(self.service.resume_recording())

        return await answering(context, body)

    async def Stop(self, request, context):
        def body():
            return convert.manifest_to_wire(self.service.stop_recording())

        return await answering(context, body)

    async def Clear(self, request, context):
        def body():
            return convert.manifest_to_wire(self.service.clear_recording())

        return await answering(context, body)

    async def ReadRecording(self, request, context):
        def body():
            return convert.manifest_to_wire(self.service.recorder.manifest_of(request.name))

        return await answering(context, body)

    async def ReadEntries(self, request, context):
        def body():
            recorder = self.service.recorder
            manifest = recorder.manifest_of(request.name)
            return convert.recording_entries_to_wire(
                request.name,
                recorder.entries_of(
                    request.name, offset=request.offset, limit=request.limit or 500
                ),
                offset=request.offset,
                entry_count=int(manifest.get("entry_count", 0)),
                segments=manifest.get("segments", []),
            )

        return await answering(context, body)

    async def DeleteRecording(self, request, context):
        def body():
            self.service.recorder.delete(request.name)
            return self._recordings()

        return await answering(context, body)
