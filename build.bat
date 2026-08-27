@echo off
rem ============================================================
rem  WeChat-Report 绿色单文件打包脚本
rem  产物：dist\WeChat-Report.exe（拷到任何 Win10/11 直接运行）
rem ============================================================
chcp 65001 >nul
where python >nul 2>nul || (echo [!] 未找到 python & exit /b 1)

pip show pyinstaller >nul 2>nul || pip install pyinstaller

echo [*] 打包中（首次约 2~4 分钟）……
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name "WeChat-Report" ^
  --collect-all rapidocr_onnxruntime ^
  --collect-all customtkinter ^
  --hidden-import PIL._tkinter_finder ^
  main.py

if exist dist\WeChat-Report.exe (
  echo [OK] 已生成 dist\WeChat-Report.exe
  echo      首次运行请把 config.example.ini 复制为 config.ini 并填好密钥
) else (
  echo [!] 打包失败，请检查上方报错
)
pause
