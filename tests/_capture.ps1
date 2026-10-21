$ErrorActionPreference = 'Continue'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$py = 'C:\Users\KK\.conda\envs\densefuse-gpu\python.exe'

Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class Win5 {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint f);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L,T,R,B; }
  public static List<IntPtr> F = new List<IntPtr>();
  public static bool Cb(IntPtr h, IntPtr l) {
    StringBuilder sb = new StringBuilder(512);
    GetWindowText(h, sb, 512);
    if (sb.ToString().Contains("EmlynDownload") && IsWindowVisible(h)) F.Add(h);
    return true;
  }
  public static void Scan() { F.Clear(); EnumWindows(new EnumProc(Cb), IntPtr.Zero); }
}
"@

function KillP {
  Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -like '*_ui_preview*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

$pairs = @(
  @('expanded', 'preview_expanded'),
  @('restored', 'preview_restored'),
  @('collapsed', 'preview_all')
)
foreach ($pair in $pairs) {
  KillP
  Start-Sleep -Seconds 2
  Start-Process -FilePath $py -ArgumentList '-X', 'utf8', 'D:\EmlynDownload\tests\_ui_preview.py', '40', $pair[0]
  Start-Sleep -Seconds 10
  [Win5]::Scan()
  if ([Win5]::F.Count -eq 0) { Write-Output "$($pair[0]) -> no window"; continue }
  $h = [Win5]::F[0]
  $r = New-Object Win5+RECT
  [void][Win5]::GetWindowRect($h, [ref]$r)
  $w = $r.R - $r.L
  $ht = $r.B - $r.T
  $bmp = New-Object System.Drawing.Bitmap($w, $ht)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $dc = $g.GetHdc()
  $ok = [Win5]::PrintWindow($h, $dc, 2)
  $g.ReleaseHdc($dc)
  $g.Dispose()
  $out = "D:\EmlynDownload\tests\$($pair[1]).png"
  $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
  $bmp.Dispose()
  Write-Output "$($pair[0]) -> $ok ${w}x${ht}"
}
KillP

Write-Output ""
Write-Output "=== clean up staging files ==="
foreach ($f in @('_chunk_taskrow.py', '_chunk_buildui.py', '_surgery.py',
                 '_EmlynDownload.bak.py', '_ui_shot.png', '_shot_collapsed.png',
                 '_shot_expanded.png', '_shot_restored.png')) {
  Remove-Item "D:\EmlynDownload\tests\$f" -Force -ErrorAction SilentlyContinue
}
Remove-Item 'D:\EmlynDownload\tests\__pycache__' -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem 'D:\EmlynDownload\tests' -File |
  Select-Object Name, @{n='KB'; e={[math]::Round($_.Length / 1KB, 1)}} |
  Format-Table -AutoSize
