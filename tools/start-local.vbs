Option Explicit

RunLauncher "start-dev.bat", "PlotPilot failed to start. Check logs or run tools\start-dev.bat from a terminal for details."

Sub RunLauncher(launcherName, failureMessage)
    Dim shell, filesystem, projectRoot, command, exitCode
    Set shell = CreateObject("WScript.Shell")
    Set filesystem = CreateObject("Scripting.FileSystemObject")
    projectRoot = filesystem.GetParentFolderName(filesystem.GetParentFolderName(WScript.ScriptFullName))
    shell.CurrentDirectory = projectRoot
    command = "cmd.exe /d /c " & Chr(34) & projectRoot & "\tools\" & launcherName & Chr(34)
    exitCode = shell.Run(command, 0, True)
    If exitCode <> 0 Then
        shell.Popup failureMessage, 0, "PlotPilot", 16
    End If
End Sub
