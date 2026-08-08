VENV ?= .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.PHONY: help install test provider-v1 provider-v2 consumer orchestrator demo \
        baseline drift agent-create agent-update agent-show voice-check clean

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t26

install:  ## create the venv and install dependencies
	python3 -m venv $(VENV) && $(PIP) install -q -r requirements.txt

test:  ## run the fenced contract test
	$(PY) -m pytest consumer/test_contract.py

provider-v1:  ## serve the baseline provider on :8001
	PROVIDER_VERSION=v1 $(VENV)/bin/uvicorn provider.app:app --port 8001 --reload

provider-v2:  ## serve the breaking provider on :8001
	PROVIDER_VERSION=v2 $(VENV)/bin/uvicorn provider.app:app --port 8001 --reload

consumer:  ## serve the customer dashboard on :8002
	$(VENV)/bin/uvicorn consumer.app:app --port 8002 --reload

orchestrator:  ## serve the orchestrator + interface on :8000
	$(VENV)/bin/uvicorn orchestrator.main:app --port 8000

demo:  ## serve the orchestrator in simulated mode
	DEMO_MODE=1 $(VENV)/bin/uvicorn orchestrator.main:app --port 8000

baseline:  ## regenerate the committed v1 baseline spec
	PROVIDER_VERSION=v1 $(PY) -m provider.export_spec > specs/provider.baseline.json

drift:  ## diff v2 against the baseline into specs/sample_drift.json
	PROVIDER_VERSION=v2 $(PY) -m provider.export_spec > /tmp/provider.v2.json
	$(PY) -m scripts.detect_drift specs/provider.baseline.json /tmp/provider.v2.json \
		--out specs/sample_drift.json

agent-create:  ## provision the CMA agent + environment (once)
	$(PY) -m orchestrator.agent_setup create

agent-update:  ## push a new agent version after editing the system prompt
	$(PY) -m orchestrator.agent_setup update

agent-show:  ## show what is configured, and verify the prompt fence
	$(PY) -m orchestrator.agent_setup show

voice-check:  ## make one real Maya call and save the fallback clip
	$(PY) -m scripts.check_voice --save-fallback

clean:
	rm -rf .pytest_cache **/__pycache__
