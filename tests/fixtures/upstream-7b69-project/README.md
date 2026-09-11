# Original upstream 7b69 client fixture

The Japanese `emuera.em` project requires core semantic/policy 3. It observes the
first duplicate alias and the valid following row, saves slot 0 through ordinary
frontend storage, checks saved version 42, clears the version after missing slot 99,
and compares constant/runtime legacy length 4 and find position 3. Persistent FLAG
watches survive INPUT replacing RESULT. Input 7 prints UPSTREAM_CONTINUED and waits.

Run the committed `upstream-7b69.json` scenario through `rustyera-test run` with an
explicit verified C ABI library. The CLI creates an isolated project copy; no save
or cache belongs in this source fixture.

`upstream-7b69-rejections.json` additionally consumes an externally prepared old
original-policy snapshot. Copy that scenario into task evidence and set its
project and snapshot path explicitly; paths are resolved relative to the
scenario. The named snapshot file is deliberately not committed or fabricated.
The rejection must have protocol command code 3 (VersionMismatch), active original identity 3/3 in protocol context,
the original full active wait/phase/epoch after frontend resynchronization, and
cleared pending import state. Both transfer and game-start rejection stages are
observed through their correlated structured events. Input 7
then proves the original wait still accepts interaction. Rejection events preserve
raw correlation ID, code and context in the trace. No localized message is used to
classify rejection; exact error text only correlates the existing worker's paired
runtime_error event with its structured import rejection event.

The optional scenario field `compiled_cache_expectation: "source_fallback"`
requires an opaque cache supplied by `RUSTYERA_TEST_COMPILED_CACHE_INPUT`, a correlated structured project report requesting source after the cache is ignored,
a successful report for the same project revision, and absence of cache-hit
diagnostics, followed by the same behavior/continuation goal. Default `"hit"` preserves old scenarios.
The driver does not manufacture an old cache or alter core compatibility checks.

Input files do not constitute acceptance evidence; final results belong in the
completed implementation record.
