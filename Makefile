.PHONY: install compile test init-db run

install:
	pip install -r requirements.txt

compile:
	python -m compileall -q .

test:
	pytest -q

init-db:
	python cli.py init-db

run:
	uvicorn app.api:app --reload
