PYTHON := python3.9
BIN := env/bin

.PHONY: env
env:
	@$(PYTHON) -m venv env
	@$(BIN)/$(PYTHON) -m pip install isort flake8 mypy black wheel
	@$(BIN)/$(PYTHON) -m pip install -r requirements.txt

.PHONY: check
check: flake8 isort-check black-check mypy-check

.PHONY: style
style: isort black

.PHONY: isort
isort:
	@$(BIN)/$(PYTHON) -m isort darkriftpy

.PHONY: black
black:
	@$(BIN)/$(PYTHON) -m black darkriftpy

.PHONY: flake8
flake8:
	@$(BIN)/$(PYTHON) -m flake8 darkriftpy

.PHONY: isort-check
isort-check:
	@$(BIN)/$(PYTHON) -m isort -c darkriftpy

.PHONY: black-check
black-check:
	@$(BIN)/$(PYTHON) -m black --check darkriftpy

.PHONY: mypy-check
mypy-check:
	@$(BIN)/$(PYTHON) -m mypy --strict darkriftpy

.PHONY: dist
dist:
	@$(BIN)/$(PYTHON) setup.py bdist_wheel bdist
