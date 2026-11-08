' ============================================================
'  PDD Seller Suite - no-black-window launcher (double-click this)
'  Launch python.exe (needs a console or Playwright Node helper
'  crashes with "Assertion failed: process_title") hidden, so the
'  Tk GUI still appears normally but no cmd window stays open.
'  Do NOT use pythonw.exe for the same reason above.
' ============================================================
Option Explicit
Dim WshShell, fso, scriptDir, PyPath, guiPath, cmd
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Prefer system Python 3.12 (has tkinter/customtkinter/jieba/playwright)
PyPath = "C:\Program Files\Python312\python.exe"
If Not fso.FileExists(PyPath) Then PyPath = "C:\Python312\python.exe"
If Not fso.FileExists(PyPath) Then PyPath = "python.exe"   ' fall back to PATH

guiPath = fso.BuildPath(scriptDir, "src\gui.py")
If Not fso.FileExists(guiPath) Then
    MsgBox "Cannot find src\gui.py. Please make sure this vbs sits in the project root next to the src folder.", vbExclamation, "Launch failed"
    WScript.Quit 1
End If

' Set working dir to project root so relative paths like data/ work
WshShell.CurrentDirectory = scriptDir

' Last arg 0 = hide python.exe's console window (process still owns a
' console so Playwright is happy); the Tk GUI pops up normally.
cmd = """" & PyPath & """ """ & guiPath & """"
WshShell.Run cmd, 0, False
