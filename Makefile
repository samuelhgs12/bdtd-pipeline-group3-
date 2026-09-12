.PHONY: demo staging test status
demo:
	python -m bdtd demo
staging:
	python -m bdtd staging
status:
	python -m bdtd status
test:
	python -m unittest discover -s tests -v
