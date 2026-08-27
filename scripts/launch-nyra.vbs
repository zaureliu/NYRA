Option Explicit

Dim shell, fileSystem, scriptDirectory, projectDirectory, powershellPath, launcherPath, command
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

scriptDirectory = fileSystem.GetParentFolderName(WScript.ScriptFullName)
projectDirectory = fileSystem.GetParentFolderName(scriptDirectory)
powershellPath = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
launcherPath = fileSystem.BuildPath(projectDirectory, "start-nyra.ps1")
command = Chr(34) & powershellPath & Chr(34) & " -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & launcherPath & Chr(34)

shell.CurrentDirectory = projectDirectory
shell.Run command, 0, False
