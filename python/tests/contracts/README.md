# Vendored from `braemons/contracts`

`outcomes.json` and `check_outcomes.py` are **copies**, byte for byte, of the
files of those names in the `braemons/contracts` repository. Nothing imports
them from anywhere; `tests/unit/test_outcomes_taxonomy.py` reads them from here.

**Copied rather than depended on, deliberately.** A shared package would make
every daemon in the family build-depend on one repository, and "small and
optional interfaces" does not survive a shared build dependency — see
`INTERACTIONS.md` §7. A repo that never syncs these files simply keeps working
against the taxonomy it last saw, and the day its own sources drift from that,
its own tests say so in its own CI.

**To update:** copy both files over from `contracts` and run the tests. If they
fail, the sources here need the new code, not the other way round.
