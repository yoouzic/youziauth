// Copyright (C) 2026 yoouzic
// SPDX-License-Identifier: GPL-3.0-only

using System;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Threading;

internal static class TrayLauncher
{
    private const string ReadyEventPrefix = @"Local\youziauth-tray-ready-";
    private static readonly TimeSpan StartupTimeout = TimeSpan.FromSeconds(30);

    public static int Main(string[] args)
    {
        string installDirectory = AppDomain.CurrentDomain.BaseDirectory;
        if (args.Length == 0)
            return Run(installDirectory, StartupTimeout);
        if (args.Length != 1 || args[0] != "--verify-runtime")
            return 2;

        try
        {
            VerifyRuntime(installDirectory);
            return 0;
        }
        catch (Exception)
        {
            return 1;
        }
    }

    internal static int Run(string installDirectory, TimeSpan timeout)
    {
        try
        {
            WaitForRuntime(installDirectory, timeout);
            string eventName = ReadyEventPrefix + Guid.NewGuid().ToString("N");
            bool created;
            using (var ready = new EventWaitHandle(false, EventResetMode.ManualReset, eventName, out created))
            {
                if (!created)
                    throw new InvalidOperationException("The tray readiness event already exists.");
                var start = new ProcessStartInfo
                {
                    FileName = Path.Combine(installDirectory, "youziauth.exe"),
                    Arguments = "--tray-startup --startup-ready-event " + eventName,
                    WorkingDirectory = installDirectory,
                    UseShellExecute = false,
                    CreateNoWindow = true
                };
                using (Process tray = Process.Start(start))
                {
                    if (tray == null)
                        throw new InvalidOperationException("Could not start the tray executable.");
                    WaitForReadiness(tray, ready, timeout);
                    tray.WaitForExit();
                    if (tray.ExitCode != 0)
                        LogFailure("Tray exited with code " + tray.ExitCode);
                    return tray.ExitCode;
                }
            }
        }
        catch (Exception error)
        {
            LogFailure(error.Message);
            return 1;
        }
    }

    private static void WaitForRuntime(string installDirectory, TimeSpan timeout)
    {
        var timer = Stopwatch.StartNew();
        while (true)
        {
            try
            {
                VerifyRuntime(installDirectory);
                return;
            }
            catch (Exception error)
            {
                if (!(error is IOException) && !(error is UnauthorizedAccessException))
                    throw;
                TimeSpan remaining = timeout - timer.Elapsed;
                if (remaining <= TimeSpan.Zero)
                    throw;
                Thread.Sleep((int)Math.Min(1000, remaining.TotalMilliseconds));
            }
        }
    }

    private static void VerifyRuntime(string installDirectory)
    {
        string runtimeDirectory = Path.Combine(installDirectory, "_internal");
        foreach (string path in new[]
        {
            Path.Combine(installDirectory, "youziauth.exe"),
            Path.Combine(runtimeDirectory, TrayRuntime.PythonDll)
        })
        {
            using (FileStream file = File.OpenRead(path))
            {
                if (file.Length == 0)
                    throw new IOException("Empty runtime file: " + path);
            }
        }

        string libraryPath = Path.Combine(runtimeDirectory, "base_library.zip");
        using (var sha = SHA256.Create())
        using (FileStream library = File.OpenRead(libraryPath))
        {
            string hash = BitConverter.ToString(sha.ComputeHash(library)).Replace("-", "");
            if (!String.Equals(hash, TrayRuntime.BaseLibraryHash, StringComparison.OrdinalIgnoreCase))
                throw new IOException("base_library.zip differs from the packaged Python runtime: " + libraryPath);
        }
    }

    private static void WaitForReadiness(Process tray, EventWaitHandle ready, TimeSpan timeout)
    {
        var timer = Stopwatch.StartNew();
        while (!ready.WaitOne(0))
        {
            if (tray.HasExited)
            {
                // A duplicate tray can signal and then exit before the next poll.
                if (ready.WaitOne(0))
                    return;
                throw new InvalidOperationException("Tray exited before readiness with code " + tray.ExitCode);
            }

            TimeSpan remaining = timeout - timer.Elapsed;
            if (remaining <= TimeSpan.Zero)
            {
                if (ready.WaitOne(0))
                    return;
                StopUnreadyTray(tray);
                throw new TimeoutException("Tray readiness timed out.");
            }
            if (ready.WaitOne((int)Math.Min(200, remaining.TotalMilliseconds)))
                return;
        }
    }

    private static void StopUnreadyTray(Process tray)
    {
        try
        {
            // Use the Process created above. A manually started tray may be running.
            tray.Kill();
        }
        catch (InvalidOperationException)
        {
            if (!tray.HasExited)
                throw;
        }
        catch (System.ComponentModel.Win32Exception)
        {
            if (!tray.HasExited)
                throw;
        }
        if (!tray.WaitForExit(5000))
            throw new TimeoutException("Tray readiness timed out and the child did not exit.");
    }

    private static void LogFailure(string reason)
    {
        try
        {
            string localAppData = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            if (String.IsNullOrEmpty(localAppData))
                localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string logs = Path.Combine(localAppData, "youziauth");
            Directory.CreateDirectory(logs);
            File.WriteAllText(Path.Combine(logs, "tray-startup-error.log"),
                DateTimeOffset.Now.ToString("o") + " " + reason + Environment.NewLine);
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }
}
