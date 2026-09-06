# The named gate. `make check` is what "green" means for this repo -- not a
# subset of the suite, and not "the tests I happened to run".
.PHONY: check mutate

check:
	uv run --extra dev pytest -q

# The falsification harness: mutate the implementation and confirm the tests
# that claim to guard it actually fail. `--spec` takes one file at a time, so
# this loops; `set -e` inside the shell fragment stops at the first spec whose
# mutations were not all caught.
mutate:
	@# Every spec, and it does NOT stop at the first survivor: knowing that one
	@# behaviour is unguarded is not a reason to stop reporting the others.
	@#
	@# `harness-selftest.json` is run apart from the rest because it is designed
	@# to produce a survivor -- a mutation nothing asserts on, proving the
	@# harness can still tell the difference. `falsify` exits non-zero whenever
	@# anything survives, so leaving that spec in the loop made this target
	@# abort on its sixth file, every time, and report the harness working as
	@# evidence that it was broken.
	@echo "== harness self-check (one mutation MUST survive)"
	@-uv run tools/falsify.py --spec tools/mutations/harness-selftest.json
	@echo
	@failed=""; \
	for spec in tools/mutations/*.json; do \
		case "$$spec" in *harness-selftest.json) continue;; esac; \
		echo "== $$spec"; \
		uv run tools/falsify.py --spec "$$spec" || failed="$$failed $$spec"; \
	done; \
	if [ -n "$$failed" ]; then \
		echo; echo "specs with a surviving mutation:$$failed"; exit 1; \
	fi; \
	echo; echo "every mutation caught"
