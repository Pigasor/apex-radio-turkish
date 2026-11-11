// Lanceur Windows d'Apex Radio : démarre l'appli (Python portable inclus) sans console.
// Compilé par GitHub Actions avec le compilateur C# de .NET Framework (C# 5, volontairement simple).
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("Apex Radio")]
[assembly: AssemblyProduct("Apex Radio")]
[assembly: AssemblyDescription("Voice race engineer for Le Mans Ultimate")]
[assembly: AssemblyCopyright("GPL-3.0")]

static class ApexRadio
{
    const string RELEASES = "https://github.com/Pigasor/apex-radio-turkish/releases";

    [STAThread]
    static int Main(string[] args)
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string[] candidates = {
            Path.Combine(dir, "python", "pythonw.exe"),
            Path.Combine(dir, ".venv", "Scripts", "pythonw.exe")
        };
        string py = null;
        foreach (string c in candidates) { if (File.Exists(c)) { py = c; break; } }
        if (py == null || !File.Exists(Path.Combine(dir, "app.py")))
        {
            MessageBox.Show("Apex Radio est incomplet : réinstalle-le.\nApex Radio is incomplete: please reinstall it.\n\n" + dir,
                            "Apex Radio", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        StringBuilder cmd = new StringBuilder("app.py");
        foreach (string a in args) { cmd.Append(" \"").Append(a.Replace("\"", "")).Append("\""); }
        ProcessStartInfo psi = new ProcessStartInfo(py, cmd.ToString());
        psi.WorkingDirectory = dir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        Process p;
        try { p = Process.Start(psi); }
        catch (Exception e)
        {
            MessageBox.Show("Impossible de lancer Apex Radio / Cannot start Apex Radio:\n" + e.Message,
                            "Apex Radio", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        // Secours : si l'appli s'arrête en erreur dans les premières secondes (mise à jour ratée…),
        // on propose d'ouvrir la page des versions pour réinstaller la précédente.
        try
        {
            if (p != null && p.WaitForExit(20000) && p.ExitCode != 0)
            {
                DialogResult r = MessageBox.Show(
                    "Apex Radio s'est arrêté au démarrage.\n\n" +
                    "Tu peux réinstaller la version précédente par-dessus : tes réglages, records et setups sont gardés.\n" +
                    "Ouvrir la page des versions ?\n\n" +
                    "Apex Radio stopped while starting.\n" +
                    "You can install the previous version over this one: your settings, records and setups are kept.\n" +
                    "Open the versions page?\n\n" +
                    "(journal.txt : %LOCALAPPDATA%\\Apex Radio)",
                    "Apex Radio", MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
                if (r == DialogResult.Yes)
                    Process.Start(new ProcessStartInfo(RELEASES) { UseShellExecute = true });
                return 1;
            }
        }
        catch (Exception) { }
        return 0;
    }
}
