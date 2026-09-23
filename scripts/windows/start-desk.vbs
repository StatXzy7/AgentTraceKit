' Launch Pair Desk as a Windows app (no console). Pin a shortcut to this file.
Option Explicit
Dim sh, root, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName)))
sh.CurrentDirectory = root
cmd = "pythonw -m agent_trace_kit.desk --app"
sh.Run cmd, 0, False
