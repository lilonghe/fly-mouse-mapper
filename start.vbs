Option Explicit
' Double click to launch the mapper with no console window.
Dim fso, sh, here, py, script
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
py = "C:\Users\m-win\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
If Not fso.FileExists(py) Then py = "pythonw.exe"
script = fso.BuildPath(here, "remapper.py")
If Not fso.FileExists(script) Then
  MsgBox "找不到 remapper.py：" & script, 16, "Air Mouse Key Mapper"
  WScript.Quit 1
End If
sh.CurrentDirectory = here
On Error Resume Next
sh.Run """" & py & """ """ & script & """", 0, False
If Err.Number <> 0 Then
  MsgBox "启动失败：" & Err.Description & vbCrLf & vbCrLf & _
         "请改用 debug.bat 查看详细错误，或确认本机已安装 Python 3。", 16, "Air Mouse Key Mapper"
  WScript.Quit 1
End If
On Error Goto 0
