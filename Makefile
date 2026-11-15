PYTHON ?= python

.PHONY: demo test fixtures serve clean

demo:
	$(PYTHON) -m pointlog demo

test:
	$(PYTHON) -m pytest -q

fixtures:
	$(PYTHON) scripts/make_fixtures.py

serve:
	$(PYTHON) -m pointlog serve --port 5000

clean:
	rm -rf var reports fixtures/replay_demo.jsonl
