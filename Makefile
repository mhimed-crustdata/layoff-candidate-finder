.PHONY: setup run demo test
setup:
	@test -f .env || cp .env.example .env; echo "Edit .env and set CRUSTDATA_API_KEY"
run:
	python3 layoff_candidates.py
demo:
	python3 layoff_candidates.py --dry-run --all
test:
	python3 -m unittest discover -s tests -v
