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
	@set -e; for spec in tools/mutations/*.json; do \
		echo "== $$spec"; \
		uv run tools/falsify.py --spec "$$spec"; \
	done
