@echo off
rem this is a simple script that builds and uploads to pip

echo cleaning...
call clean.bat

echo building distributions...
python setup.py sdist bdist_wheel

echo Pausing. Inspect the above output. Ctrl-C to terminate if you have reservations.
pause

echo Uploading...
twine upload dist/*

echo Upload complete!
pause