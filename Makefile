.PHONY: test build check clean publish-test publish install

test:
	python -m unittest discover -s tests

build: clean
	python -m build

check: build
	twine check dist/*

install:
	pip install -e .

publish-test: check
	twine upload --repository testpypi dist/*

publish: check
	twine upload dist/*

clean:
	python -c "import shutil, glob; [shutil.rmtree(p, ignore_errors=True) for p in ['dist', 'build', *glob.glob('src/*.egg-info')]]"