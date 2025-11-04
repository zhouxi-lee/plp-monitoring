@echo off
SETLOCAL
echo [1/3] Creating/activating venv...
if not exist .venv (
  python -m venv .venv
)
call .venv\Scripts\activate.bat
echo [2/3] Installing deps...
pip install -r requirements.txt
echo [3/3] Installing Playwright browser (one-time)...
python -m playwright install chromium
echo Starting Streamlit...
streamlit run app.py
ENDLOCAL
