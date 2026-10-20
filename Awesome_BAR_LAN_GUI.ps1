# Simple GUI front end for the known-good Beyond All Reason direct LAN host/join scripts.
# Compatible with Windows PowerShell 5.1 and the BAR Windows desktop installation.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

# Reads the installed map list and minimap icons through the engine's own unitsync.dll (64-bit PowerShell required).
if (-not ('UnitSyncBridge' -as [type])) {
Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

public static class UnitSyncBridge
{
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool SetDllDirectory(string path);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr LoadLibrary(string path);
    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, ExactSpelling = true)]
    static extern IntPtr GetProcAddress(IntPtr module, string name);

    delegate int InitFn(byte isServer, int id);
    delegate void UnInitFn();
    delegate int CountFn();
    delegate IntPtr NameFn(int index);
    delegate IntPtr MinimapFn(IntPtr mapName, int mip);
    delegate IntPtr ErrFn();

    static IntPtr lib = IntPtr.Zero;
    static bool ready = false;
    static InitFn initFn;
    static UnInitFn uninitFn;
    static CountFn countFn;
    static NameFn nameFn;
    static MinimapFn minimapFn;
    static ErrFn errFn;

    static T Bind<T>(string name) where T : class
    {
        IntPtr p = GetProcAddress(lib, name);
        if (p == IntPtr.Zero) return null;
        return Marshal.GetDelegateForFunctionPointer(p, typeof(T)) as T;
    }

    static string Utf8(IntPtr p)
    {
        if (p == IntPtr.Zero) return null;
        int n = 0;
        while (Marshal.ReadByte(p, n) != 0) n++;
        byte[] b = new byte[n];
        Marshal.Copy(p, b, 0, n);
        return Encoding.UTF8.GetString(b);
    }

    static string LastError()
    {
        try { return errFn == null ? "unknown error" : Utf8(errFn()); } catch { return "unknown error"; }
    }

    public static string Open(string engineDir, string dataDir)
    {
        if (!Environment.Is64BitProcess) return "This needs 64-bit PowerShell (the default powershell.exe on 64-bit Windows).";
        if (!ready)
        {
            string dll = Path.Combine(engineDir, "unitsync.dll");
            if (!File.Exists(dll)) return "unitsync.dll was not found in: " + engineDir;
            Environment.SetEnvironmentVariable("SPRING_DATADIR", dataDir);
            SetDllDirectory(engineDir);
            lib = LoadLibrary(dll);
            if (lib == IntPtr.Zero) return "Could not load unitsync.dll (Windows error " + Marshal.GetLastWin32Error() + ").";
            initFn = Bind<InitFn>("Init");
            uninitFn = Bind<UnInitFn>("UnInit");
            countFn = Bind<CountFn>("GetMapCount");
            nameFn = Bind<NameFn>("GetMapName");
            minimapFn = Bind<MinimapFn>("GetMinimap");
            errFn = Bind<ErrFn>("GetNextError");
            if (initFn == null || countFn == null || nameFn == null || minimapFn == null) return "unitsync.dll is missing expected functions.";
            ready = true;
        }
        if (initFn(0, 0) == 0) return "unitsync Init failed: " + LastError();
        return null;
    }

    public static List<string> MapNames()
    {
        List<string> list = new List<string>();
        int n = countFn();
        for (int i = 0; i < n; i++)
        {
            string name = Utf8(nameFn(i));
            if (!string.IsNullOrEmpty(name) && !list.Contains(name)) list.Add(name);
        }
        list.Sort(StringComparer.OrdinalIgnoreCase);
        return list;
    }

    // mip 3 => 128x128 icon
    public static Bitmap Minimap(string mapName, int mip)
    {
        byte[] nb = Encoding.UTF8.GetBytes(mapName + "\0");
        IntPtr np = Marshal.AllocHGlobal(nb.Length);
        IntPtr px;
        try { Marshal.Copy(nb, 0, np, nb.Length); px = minimapFn(np, mip); }
        finally { Marshal.FreeHGlobal(np); }
        if (px == IntPtr.Zero) return null;
        int size = 1024 >> mip;
        short[] data = new short[size * size];
        Marshal.Copy(px, data, 0, data.Length);
        Bitmap bmp = new Bitmap(size, size, System.Drawing.Imaging.PixelFormat.Format24bppRgb);
        for (int y = 0; y < size; y++)
        {
            for (int x = 0; x < size; x++)
            {
                int v = (ushort)data[y * size + x];
                int r = ((v >> 11) & 31) * 255 / 31;
                int g = ((v >> 5) & 63) * 255 / 63;
                int b = (v & 31) * 255 / 31;
                bmp.SetPixel(x, y, Color.FromArgb(r, g, b));
            }
        }
        return bmp;
    }

    public static void Close()
    {
        if (ready && uninitFn != null) { try { uninitFn(); } catch { } }
    }
}
'@
}

$script:appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:settingsPath = Join-Path $script:appRoot 'awesome_bar_lan_settings.json'
$defaultDataDir = Join-Path $env:LOCALAPPDATA 'Programs\Beyond-All-Reason\data'
$defaultEngineDir = Join-Path $defaultDataDir 'engine\recoil_2026.09.01'

$script:initialDataDir = $defaultDataDir
$script:initialEngineDir = $defaultEngineDir
if (Test-Path -LiteralPath $script:settingsPath) {
    try {
        $savedSettings = Get-Content -LiteralPath $script:settingsPath -Raw | ConvertFrom-Json
        if ($savedSettings.DataDir) { $script:initialDataDir = [string]$savedSettings.DataDir }
        if ($savedSettings.EngineDir) { $script:initialEngineDir = [string]$savedSettings.EngineDir }
    } catch { }
}

function Get-LanIPv4 {
    $found = @()
    try {
        foreach ($nic in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
            if ($nic.OperationalStatus -ne [System.Net.NetworkInformation.OperationalStatus]::Up) { continue }
            if ($nic.NetworkInterfaceType -eq [System.Net.NetworkInformation.NetworkInterfaceType]::Loopback -or
                $nic.NetworkInterfaceType -eq [System.Net.NetworkInformation.NetworkInterfaceType]::Tunnel) { continue }
            $hasGateway = $false
            try {
                foreach ($gw in $nic.GetIPProperties().GatewayAddresses) {
                    if ($gw.Address -and $gw.Address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and $gw.Address.ToString() -ne '0.0.0.0') { $hasGateway = $true }
                }
            } catch { }
            foreach ($ua in $nic.GetIPProperties().UnicastAddresses) {
                $a = $ua.Address
                if ($a.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { continue }
                $s = $a.ToString()
                if ($s -like '127.*' -or $s -like '169.254.*' -or $s -eq '0.0.0.0') { continue }
                $found += [pscustomobject]@{ Address = $s; Gateway = $hasGateway; Name = $nic.Name }
            }
        }
    } catch { }
    $best = $found | Sort-Object @{ Expression = { if ($_.Gateway) { 0 } else { 1 } } }, Name | Select-Object -First 1
    if ($best) { return $best.Address }
    return ''
}

function Get-PublicIPv4 {
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
    foreach ($url in @('https://api.ipify.org', 'https://ifconfig.me/ip', 'https://icanhazip.com')) {
        try {
            $r = ([string](Invoke-RestMethod -Uri $url -TimeoutSec 6)).Trim()
            $ip = $null
            if ([System.Net.IPAddress]::TryParse($r, [ref]$ip) -and $ip.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) { return $r }
        } catch { }
    }
    return ''
}

function Get-ProtonNic {
    try {
        foreach ($nic in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
            if ($nic.OperationalStatus -ne [System.Net.NetworkInformation.OperationalStatus]::Up) { continue }
            if ($nic.Name -match 'proton' -or $nic.Description -match 'proton') { return $nic }
        }
    } catch { }
    return $null
}

function Get-ProtonGateways($nic) {
    $list = @()
    try {
        foreach ($gw in $nic.GetIPProperties().GatewayAddresses) {
            if ($gw.Address -and $gw.Address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and $gw.Address.ToString() -ne '0.0.0.0') { $list += $gw.Address.ToString() }
        }
    } catch { }
    foreach ($d in @('10.2.0.1', '10.96.0.1')) { if ($list -notcontains $d) { $list += $d } }
    return $list
}

# NAT-PMP (RFC 6886) UDP mapping request, used by Proton VPN port forwarding. Returns the assigned port, or 0 on failure.
function Request-ProtonPortForward([string[]]$gateways) {
    $req = [byte[]](0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 60)
    foreach ($gw in $gateways) {
        $udp = New-Object System.Net.Sockets.UdpClient
        try {
            $udp.Client.ReceiveTimeout = 2000
            $udp.Connect($gw, 5351)
            [void]$udp.Send($req, $req.Length)
            $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
            $resp = $udp.Receive([ref]$ep)
            if ($resp.Length -ge 16 -and $resp[1] -eq 129 -and $resp[2] -eq 0 -and $resp[3] -eq 0) {
                $mapped = [int]($resp[10] * 256 + $resp[11])
                if ($mapped -gt 0) { return $mapped }
            }
        } catch { } finally { $udp.Close() }
    }
    return 0
}

# Decides how friends reach you: Proton VPN (with port forwarding) if connected, otherwise your direct public IP.
function Resolve-HostEndpoint {
    $nic = Get-ProtonNic
    if ($nic) {
        $gws = Get-ProtonGateways $nic
        $port = Request-ProtonPortForward $gws
        if ($port -le 0) {
            throw "Proton VPN is connected, but port forwarding is not active, so friends cannot reach you through it.`r`n`r`nIn the Proton VPN app: turn on Port Forwarding in Settings (paid plan), then connect to a P2P server and try again.`r`nOr disconnect Proton VPN to host using your own public IP."
        }
        $ip = Get-PublicIPv4
        if (-not $ip) { throw 'Proton VPN is connected but the VPN exit IP could not be looked up.' }
        $script:pmpGateways = $gws
        $script:tbHostPort.Text = "$port"
        if (-not $script:pmpTimer) {
            $script:pmpTimer = New-Object System.Windows.Forms.Timer
            $script:pmpTimer.Interval = 45000
            $script:pmpTimer.Add_Tick({
                if (-not (Get-ProtonNic)) { $script:pmpTimer.Stop(); return }
                $p = Request-ProtonPortForward $script:pmpGateways
                if ($p -gt 0 -and "$p" -ne $script:tbHostPort.Text.Trim()) {
                    $script:tbHostPort.Text = "$p"
                    Set-Status "Proton forwarded port changed to $p. Restart hosting and resend the invite."
                }
            })
        }
        $script:pmpTimer.Start()
        return [pscustomobject]@{ Mode = 'proton'; Ip = $ip; Port = $port }
    }
    if ($script:pmpTimer) { $script:pmpTimer.Stop() }
    $ip = Get-PublicIPv4
    if (-not $ip) { throw 'Could not detect your public IP. Check your internet connection.' }
    return [pscustomobject]@{ Mode = 'direct'; Ip = $ip; Port = (Test-Port $script:tbHostPort.Text.Trim()) }
}

function Show-Message([string]$message, [string]$title = 'Awesome BAR LAN', [System.Windows.Forms.MessageBoxIcon]$icon = [System.Windows.Forms.MessageBoxIcon]::Information) {
    [void][System.Windows.Forms.MessageBox]::Show($script:form, $message, $title, [System.Windows.Forms.MessageBoxButtons]::OK, $icon)
}

function Set-Status([string]$message) {
    if ($script:statusLabel) { $script:statusLabel.Text = $message }
}

function Test-TextValue([string]$value, [string]$label) {
    if ([string]::IsNullOrWhiteSpace($value)) { throw "$label is required." }
    if ($value -match '[;\r\n{}]') { throw "$label cannot contain semicolons, braces, or line breaks." }
}

function Test-Port([string]$portText) {
    $p = 0
    if (-not [int]::TryParse($portText, [ref]$p) -or $p -lt 1 -or $p -gt 65535) { throw 'Port must be a whole number from 1 to 65535.' }
    return $p
}

function Get-EngineExe {
    $dataDir = $script:tbDataDir.Text.Trim()
    $engineDir = $script:tbEngineDir.Text.Trim()
    if (-not (Test-Path -LiteralPath $dataDir -PathType Container)) {
        throw "BAR data folder does not exist:`r`n$dataDir`r`nOpen Settings and choose the correct data folder."
    }
    if ($engineDir -and (Test-Path -LiteralPath (Join-Path $engineDir 'spring.exe') -PathType Leaf)) {
        return (Join-Path $engineDir 'spring.exe')
    }
    $engineRoot = Join-Path $dataDir 'engine'
    if (Test-Path -LiteralPath $engineRoot -PathType Container) {
        $matches = @(Get-ChildItem -LiteralPath $engineRoot -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { $exe = Join-Path $_.FullName 'spring.exe'; if (Test-Path -LiteralPath $exe -PathType Leaf) { Get-Item -LiteralPath $exe } } |
            Sort-Object LastWriteTime -Descending)
        if ($matches.Count -gt 0) {
            $script:tbEngineDir.Text = Split-Path -Parent $matches[0].FullName
            return $matches[0].FullName
        }
    }
    throw "Could not find spring.exe. Current engine folder:`r`n$engineDir`r`nUse Settings → Browse next to Engine folder."
}

function Get-GeneratedFolder {
    $p = Join-Path $script:appRoot 'GeneratedScripts'
    if (-not (Test-Path -LiteralPath $p -PathType Container)) { [void](New-Item -ItemType Directory -Path $p -Force) }
    return $p
}

function Build-HostScriptText {
    $hostName = $script:tbHostName.Text.Trim()
    $mapName = $script:tbMapName.Text.Trim()
    $friendName = $script:tbFriendName.Text.Trim()
    $password = $script:tbHostPassword.Text
    $port = Test-Port $script:tbHostPort.Text.Trim()
    Test-TextValue $hostName 'Host player name'
    Test-TextValue $mapName 'Map name'
    Test-TextValue $friendName 'Friend player name'
    Test-TextValue $password 'Friend password'
    if ($mapName -match '\.(sd7|sdz|smf)$') { throw 'Enter the map title as it appears in the BAR map list, without .sd7 or .smf. Example: All That Simmers v1.1.1' }
    return @"
// Generated by Awesome BAR LAN GUI. MapName is the in-game map title, with no file extension.
[GAME]
{
    GameType=Beyond All Reason `$VERSION;
    MapName=$mapName;

    IsHost=1;
    HostIP=;
    HostPort=$port;
    MyPlayerName=$hostName;

    StartPosType=2;
    NumPlayers=2;
    NumTeams=2;
    NumAllyTeams=2;

    [PLAYER0]
    {
        Name=$hostName;
        Team=0;
        Spectator=0;
    }

    [PLAYER1]
    {
        Name=$friendName;
        Password=$password;
        Team=1;
        Spectator=0;
    }

    [TEAM0]
    {
        TeamLeader=0;
        AllyTeam=0;
        Side=Awesome;
        RgbColor=0.9 0.6 0.2;
    }

    [TEAM1]
    {
        TeamLeader=1;
        AllyTeam=1;
        Side=Awesome;
        RgbColor=0.2 0.5 0.9;
    }

    [ALLYTEAM0]
    {
        NumAllies=0;
        StartRectLeft=0;
        StartRectTop=0;
        StartRectRight=1;
        StartRectBottom=0.2;
    }

    [ALLYTEAM1]
    {
        NumAllies=0;
        StartRectLeft=0;
        StartRectTop=0.8;
        StartRectRight=1;
        StartRectBottom=1;
    }
}
"@
}

function Build-ClientScriptText {
    $hostIp = $script:tbJoinHostIP.Text.Trim()
    $port = Test-Port $script:tbJoinPort.Text.Trim()
    $playerName = $script:tbJoinName.Text.Trim()
    $password = $script:tbJoinPassword.Text
    Test-TextValue $playerName 'Your in-game player name'
    Test-TextValue $password 'Password'
    if ([string]::IsNullOrWhiteSpace($hostIp)) { throw 'Enter the host computer’s IPv4 address (public IP for internet play).' }
    $parsedIp = $null
    if (-not [System.Net.IPAddress]::TryParse($hostIp, [ref]$parsedIp) -or $parsedIp.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        throw 'Host IP must be an IPv4 address, for example 192.168.1.25.'
    }
    return @"
// Generated by Awesome BAR LAN GUI. The host IP is the host's public IPv4 for internet play (LAN IP only for same-network play).
[GAME]
{
    HostIP=$hostIp;
    HostPort=$port;
    SourcePort=0;
    MyPlayerName=$playerName;
    MyPasswd=$password;
    IsHost=0;
}
"@
}

function Write-Utf8NoBom([string]$path, [string]$content) {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $content, $enc)
}

function Save-HostScript {
    $text = Build-HostScriptText
    $path = Join-Path (Get-GeneratedFolder) 'awesome_host_script.txt'
    Write-Utf8NoBom $path $text
    $script:lastHostScriptPath = $path
    Set-Status "Host script saved: $path"
    return $path
}

function Save-ClientScript {
    $text = Build-ClientScriptText
    $path = Join-Path (Get-GeneratedFolder) 'awesome_client_script.txt'
    Write-Utf8NoBom $path $text
    $script:lastClientScriptPath = $path
    Set-Status "Join script saved: $path"
    return $path
}

function Launch-EngineScript([string]$scriptPath, [string]$activity) {
    $engineExe = Get-EngineExe
    $dataDir = $script:tbDataDir.Text.Trim()
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw "Generated start script was not found:`r`n$scriptPath" }
    $argLine = '--write-dir "{0}" "{1}"' -f $dataDir, $scriptPath
    Start-Process -FilePath $engineExe -ArgumentList $argLine -WorkingDirectory (Split-Path -Parent $engineExe) | Out-Null
    Set-Status "$activity started. If BAR opens but fails to connect, check script values and Windows Firewall."
}

function Save-AppSettings {
    $dataDir = $script:tbDataDir.Text.Trim()
    $engineDir = $script:tbEngineDir.Text.Trim()
    if ([string]::IsNullOrWhiteSpace($dataDir)) { throw 'Choose the BAR data folder first.' }
    if (-not (Test-Path -LiteralPath $dataDir -PathType Container)) { throw "BAR data folder does not exist:`r`n$dataDir" }
    $obj = [pscustomobject]@{ DataDir = $dataDir; EngineDir = $engineDir }
    Write-Utf8NoBom $script:settingsPath ($obj | ConvertTo-Json)
    Set-Status 'Settings saved.'
}

# ---------- Map list + icons ----------
$script:mapIcons = @{}

function Get-MapCacheFolder {
    $p = Join-Path $script:appRoot 'MapCache'
    if (-not (Test-Path -LiteralPath $p -PathType Container)) { [void](New-Item -ItemType Directory -Path $p -Force) }
    return $p
}

function Get-MapIconPath([string]$mapName) {
    return Join-Path (Get-MapCacheFolder) (($mapName -replace '[^A-Za-z0-9._-]', '_') + '.png')
}

function Set-MapComboItems([string[]]$names) {
    $current = $script:tbMapName.Text
    $script:tbMapName.Items.Clear()
    foreach ($n in $names) { [void]$script:tbMapName.Items.Add($n) }
    $script:tbMapName.Text = $current
}

function Import-MapCache {
    $listPath = Join-Path (Get-MapCacheFolder) 'maps.json'
    if (-not (Test-Path -LiteralPath $listPath)) { return $false }
    try {
        $names = @(Get-Content -LiteralPath $listPath -Raw | ConvertFrom-Json)
        $script:mapIcons = @{}
        foreach ($n in $names) {
            $f = Get-MapIconPath $n
            if (Test-Path -LiteralPath $f) {
                $img = [System.Drawing.Image]::FromFile($f)
                try { $script:mapIcons[[string]$n] = New-Object System.Drawing.Bitmap($img) } finally { $img.Dispose() }
            }
        }
        Set-MapComboItems ([string[]]$names)
        return ($names.Count -gt 0)
    } catch { return $false }
}

function Update-MapListFromGame {
    $engineExe = Get-EngineExe
    $engineDir = Split-Path -Parent $engineExe
    $dataDir = $script:tbDataDir.Text.Trim()
    Set-Status 'Scanning installed maps (the first scan can take a while)...'
    $script:form.Cursor = [System.Windows.Forms.Cursors]::WaitCursor
    $script:form.Refresh()
    try {
        $err = [UnitSyncBridge]::Open($engineDir, $dataDir)
        if ($err) { throw $err }
        $names = @([UnitSyncBridge]::MapNames())
        $icons = @{}
        foreach ($n in $names) {
            $bmp = [UnitSyncBridge]::Minimap($n, 3)
            if ($bmp) {
                $icons[$n] = $bmp
                try { $bmp.Save((Get-MapIconPath $n), [System.Drawing.Imaging.ImageFormat]::Png) } catch { }
            }
        }
    } finally {
        [UnitSyncBridge]::Close()
        $script:form.Cursor = [System.Windows.Forms.Cursors]::Default
    }
    $script:mapIcons = $icons
    $json = ConvertTo-Json -InputObject @($names)
    Write-Utf8NoBom (Join-Path (Get-MapCacheFolder) 'maps.json') $json
    Set-MapComboItems ([string[]]$names)
    & $script:updateMapPreview
    Set-Status "Found $($names.Count) installed maps."
}

# ---------- GUI helpers ----------
function Add-Label([System.Windows.Forms.Control]$parent, [string]$text, [int]$x, [int]$y, [int]$w = 180, [int]$h = 25) {
    $c = New-Object System.Windows.Forms.Label
    $c.Text = $text; $c.Location = New-Object System.Drawing.Point($x, $y); $c.Size = New-Object System.Drawing.Size($w, $h)
    $c.Font = New-Object System.Drawing.Font('Segoe UI', 9)
    $parent.Controls.Add($c); return $c
}
function Add-TextBox([System.Windows.Forms.Control]$parent, [string]$name, [string]$value, [int]$x, [int]$y, [int]$w = 300, [bool]$isPassword = $false) {
    $c = New-Object System.Windows.Forms.TextBox
    $c.Name = $name; $c.Text = $value; $c.Location = New-Object System.Drawing.Point($x, $y); $c.Size = New-Object System.Drawing.Size($w, 27)
    $c.Font = New-Object System.Drawing.Font('Segoe UI', 9.5)
    if ($isPassword) { $c.UseSystemPasswordChar = $true }
    $parent.Controls.Add($c); return $c
}
function Add-Button([System.Windows.Forms.Control]$parent, [string]$text, [int]$x, [int]$y, [int]$w = 140, [int]$h = 34) {
    $c = New-Object System.Windows.Forms.Button
    $c.Text = $text; $c.Location = New-Object System.Drawing.Point($x, $y); $c.Size = New-Object System.Drawing.Size($w, $h)
    $c.Font = New-Object System.Drawing.Font('Segoe UI', 9, [System.Drawing.FontStyle]::Regular)
    $parent.Controls.Add($c); return $c
}
function Show-UiError($ex) { Show-Message $ex.Exception.Message 'Could not complete action' ([System.Windows.Forms.MessageBoxIcon]::Error) }

# ---------- Main window ----------
$script:form = New-Object System.Windows.Forms.Form
$script:form.Text = 'Awesome BAR LAN Lobby'
$script:form.StartPosition = 'CenterScreen'
$script:form.ClientSize = New-Object System.Drawing.Size(900, 650)
$script:form.MinimumSize = New-Object System.Drawing.Size(850, 650)
$script:form.Font = New-Object System.Drawing.Font('Segoe UI', 9)
$script:form.BackColor = [System.Drawing.Color]::FromArgb(242, 245, 249)

$header = New-Object System.Windows.Forms.Panel
$header.Location = New-Object System.Drawing.Point(0, 0); $header.Size = New-Object System.Drawing.Size(900, 72)
$header.BackColor = [System.Drawing.Color]::FromArgb(34, 48, 67)
$script:form.Controls.Add($header)
$headTitle = Add-Label $header 'AWESOME BAR LAN LOBBY' 22 12 520 30
$headTitle.ForeColor = [System.Drawing.Color]::White
$headTitle.Font = New-Object System.Drawing.Font('Segoe UI Semibold', 17, [System.Drawing.FontStyle]::Bold)
$headSub = Add-Label $header 'Simple direct-connect host and join launcher for your Awesome faction mod' 24 43 810 20
$headSub.ForeColor = [System.Drawing.Color]::FromArgb(222, 231, 241)

$script:tabs = New-Object System.Windows.Forms.TabControl
$script:tabs.Location = New-Object System.Drawing.Point(16, 86); $script:tabs.Size = New-Object System.Drawing.Size(868, 500)
$script:tabs.Font = New-Object System.Drawing.Font('Segoe UI', 9.5)
$script:form.Controls.Add($script:tabs)
$tabHost = New-Object System.Windows.Forms.TabPage; $tabHost.Text = 'Host a match'; $tabHost.BackColor = [System.Drawing.Color]::White
$tabJoin = New-Object System.Windows.Forms.TabPage; $tabJoin.Text = 'Join a match'; $tabJoin.BackColor = [System.Drawing.Color]::White
$tabSettings = New-Object System.Windows.Forms.TabPage; $tabSettings.Text = 'Settings'; $tabSettings.BackColor = [System.Drawing.Color]::White
[void]$script:tabs.TabPages.Add($tabHost); [void]$script:tabs.TabPages.Add($tabJoin); [void]$script:tabs.TabPages.Add($tabSettings)

# Host tab
Add-Label $tabHost 'Your in-game player name' 25 24 220 | Out-Null
$script:tbHostName = Add-TextBox $tabHost 'tbHostName' 'gonj183notAlex' 255 21 330
Add-Label $tabHost 'Map (pick from list or type title)' 25 70 245 | Out-Null
$script:tbMapName = New-Object System.Windows.Forms.ComboBox
$script:tbMapName.Name = 'tbMapName'
$script:tbMapName.Location = New-Object System.Drawing.Point(255, 64)
$script:tbMapName.Size = New-Object System.Drawing.Size(500, 44)
$script:tbMapName.Font = New-Object System.Drawing.Font('Segoe UI', 9.5)
$script:tbMapName.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDown
$script:tbMapName.DrawMode = [System.Windows.Forms.DrawMode]::OwnerDrawFixed
$script:tbMapName.ItemHeight = 44
$script:tbMapName.DropDownHeight = 440
$script:tbMapName.Text = 'All That Simmers v1.1.1'
$tabHost.Controls.Add($script:tbMapName)
$script:tbMapName.Add_DrawItem({
    param($sender, $e)
    if ($e.Index -lt 0) { return }
    $e.DrawBackground()
    $name = [string]$sender.Items[$e.Index]
    $icon = $script:mapIcons[$name]
    if ($icon) { $e.Graphics.DrawImage($icon, ($e.Bounds.X + 3), ($e.Bounds.Y + 3), 38, 38) }
    $brush = New-Object System.Drawing.SolidBrush($e.ForeColor)
    $e.Graphics.DrawString($name, $sender.Font, $brush, ($e.Bounds.X + 50), ($e.Bounds.Y + 13))
    $brush.Dispose()
    $e.DrawFocusRectangle()
})
$script:picMap = New-Object System.Windows.Forms.PictureBox
$script:picMap.Location = New-Object System.Drawing.Point(770, 52)
$script:picMap.Size = New-Object System.Drawing.Size(70, 70)
$script:picMap.SizeMode = [System.Windows.Forms.PictureBoxSizeMode]::Zoom
$script:picMap.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$tabHost.Controls.Add($script:picMap)
$script:updateMapPreview = {
    $n = $script:tbMapName.Text.Trim()
    if ($n -and $script:mapIcons.ContainsKey($n)) { $script:picMap.Image = $script:mapIcons[$n] } else { $script:picMap.Image = $null }
}
$script:tbMapName.Add_TextChanged($script:updateMapPreview)
$script:tbMapName.Add_SelectedIndexChanged($script:updateMapPreview)
$btnRefreshMaps = Add-Button $tabHost 'Refresh map list' 600 110 155 30
$btnRefreshMaps.Add_Click({ try { Update-MapListFromGame } catch { Show-UiError $_ } })
Add-Label $tabHost 'Friend player name' 25 116 220 | Out-Null
$script:tbFriendName = Add-TextBox $tabHost 'tbFriendName' 'FriendName' 255 113 330
Add-Label $tabHost 'Friend password' 25 162 220 | Out-Null
$script:tbHostPassword = Add-TextBox $tabHost 'tbHostPassword' 'awe123' 255 159 330 $true
Add-Label $tabHost 'Host port' 25 208 220 | Out-Null
$script:tbHostPort = Add-TextBox $tabHost 'tbHostPort' '8452' 255 205 120
$hintHost = Add-Label $tabHost 'Map title must match BAR. Both need the same mod and map. For internet play: with Proton VPN, enable Port Forwarding and use a P2P server (the port is set automatically); without it, forward the UDP port on your router.' 25 240 795 48
$hintHost.ForeColor = [System.Drawing.Color]::FromArgb(80, 90, 104)
$ipPanel = New-Object System.Windows.Forms.GroupBox; $ipPanel.Text = 'Address for far-away friends (Proton VPN if connected, otherwise public IP)'; $ipPanel.Location = New-Object System.Drawing.Point(25, 294); $ipPanel.Size = New-Object System.Drawing.Size(795, 100); $tabHost.Controls.Add($ipPanel)
$script:lblLanIP = Add-Label $ipPanel ('Your LAN IP: ' + (Get-LanIPv4)) 12 25 300 25
$script:lblLanIP.Font = New-Object System.Drawing.Font('Segoe UI Semibold', 10, [System.Drawing.FontStyle]::Bold)
$script:lblPublicIP = Add-Label $ipPanel 'Address: (click Detect)' 12 62 400 25
$script:lblPublicIP.Font = New-Object System.Drawing.Font('Segoe UI Semibold', 10, [System.Drawing.FontStyle]::Bold)
$btnRefreshIP = Add-Button $ipPanel 'Refresh LAN IP' 430 20 130 32
$btnDetectPublic = Add-Button $ipPanel 'Detect address' 430 56 130 32
$btnCopyInvite = Add-Button $ipPanel 'Copy invite' 570 56 210 32
$btnRefreshIP.Add_Click({ $ip = Get-LanIPv4; $script:lblLanIP.Text = "Your LAN IP: $ip"; if ($ip) { Set-Status "Detected LAN address $ip" } else { Set-Status 'No LAN IPv4 address found. Connect to Wi-Fi or Ethernet and refresh.' } })
$btnDetectPublic.Add_Click({
    try {
        Set-Status 'Detecting connection (Proton VPN or direct IP)...'
        $script:form.Refresh()
        $ep = Resolve-HostEndpoint
        if ($ep.Mode -eq 'proton') {
            $script:lblPublicIP.Text = "Proton VPN: $($ep.Ip) : $($ep.Port)"
            Set-Status "Using Proton VPN port forwarding. Friends connect to $($ep.Ip) port $($ep.Port)."
        } else {
            $script:lblPublicIP.Text = "Public IP: $($ep.Ip)"
            Set-Status "No Proton VPN detected. Using your public IP. Forward UDP $($ep.Port) on your router."
        }
    } catch { Show-UiError $_ }
})
$btnCopyInvite.Add_Click({
    try {
        $ep = Resolve-HostEndpoint
        $ip = $ep.Ip
        if ($ep.Mode -eq 'proton') { $script:lblPublicIP.Text = "Proton VPN: $ip : $($ep.Port)" } else { $script:lblPublicIP.Text = "Public IP: $ip" }
        $invite = "Awesome BAR match`r`nHost IP: $ip`r`nPort: $($script:tbHostPort.Text.Trim())`r`nMap: $($script:tbMapName.Text.Trim())`r`nYour player name: $($script:tbFriendName.Text.Trim())`r`nPassword: $($script:tbHostPassword.Text)"
        [System.Windows.Forms.Clipboard]::SetText($invite)
        Set-Status 'Invite copied. Send it privately. (Direct IP mode needs router port forwarding.)'
    } catch { Show-UiError $_ }
})
$btnSaveHost = Add-Button $tabHost 'Save host script' 25 410 150 38
$btnStartHost = Add-Button $tabHost 'START HOSTING' 190 410 180 38
$btnOpenGen = Add-Button $tabHost 'Open generated scripts' 385 410 180 38
$btnFirewall = Add-Button $tabHost 'Allow port in firewall' 580 410 200 38
$btnFirewall.Add_Click({
    try {
        $port = Test-Port $script:tbHostPort.Text.Trim()
        $cmd = "New-NetFirewallRule -DisplayName 'Awesome BAR host UDP $port' -Direction Inbound -Protocol UDP -LocalPort $port -Action Allow -Profile Any | Out-Null"
        Start-Process powershell.exe -Verb RunAs -ArgumentList '-NoProfile', '-Command', $cmd
        Set-Status "Requested Windows Firewall rule for inbound UDP $port (approve the admin prompt)."
    } catch { Show-UiError $_ }
})
$btnSaveHost.Add_Click({ try { $p = Save-HostScript; Show-Message "Host script saved to:`r`n$p" } catch { Show-UiError $_ } })
$btnStartHost.Add_Click({ try { [void](Resolve-HostEndpoint); $p = Save-HostScript; Launch-EngineScript $p 'Host' } catch { Show-UiError $_ } })
$btnOpenGen.Add_Click({ try { $p = Get-GeneratedFolder; Start-Process explorer.exe -ArgumentList "`"$p`"" } catch { Show-UiError $_ } })

# Join tab
Add-Label $tabJoin 'Host IPv4 address (public IP for internet)' 25 30 245 | Out-Null
$script:tbJoinHostIP = Add-TextBox $tabJoin 'tbJoinHostIP' '' 270 27 250
Add-Label $tabJoin 'Host port' 25 78 220 | Out-Null
$script:tbJoinPort = Add-TextBox $tabJoin 'tbJoinPort' '8452' 270 75 120
Add-Label $tabJoin 'Your in-game player name' 25 126 240 | Out-Null
$script:tbJoinName = Add-TextBox $tabJoin 'tbJoinName' 'FriendName' 270 123 330
Add-Label $tabJoin 'Match password' 25 174 220 | Out-Null
$script:tbJoinPassword = Add-TextBox $tabJoin 'tbJoinPassword' 'awe123' 270 171 330 $true
$joinHelp = Add-Label $tabJoin "Use the host's public IP (or LAN IP if on the same network), port, player name, and password from their invite. Your player name and password must exactly match the values assigned for you by the host." 25 225 790 62
$joinHelp.ForeColor = [System.Drawing.Color]::FromArgb(80, 90, 104)
$btnSaveJoin = Add-Button $tabJoin 'Save join script' 25 315 150 38
$btnJoin = Add-Button $tabJoin 'JOIN MATCH' 190 315 180 38
$btnRefreshJoinIP = Add-Button $tabJoin 'Show my LAN IP' 385 315 150 38
$btnSaveJoin.Add_Click({ try { $p = Save-ClientScript; Show-Message "Join script saved to:`r`n$p" } catch { Show-UiError $_ } })
$btnJoin.Add_Click({ try { $p = Save-ClientScript; Launch-EngineScript $p 'Join' } catch { Show-UiError $_ } })
$btnRefreshJoinIP.Add_Click({ $ip = Get-LanIPv4; if ($ip) { Show-Message "This computer's LAN IP is $ip.`r`nTo join a friend, enter THEIR LAN IP in the Host IPv4 field." } else { Show-Message 'No LAN IPv4 address was found. Connect to Wi-Fi or Ethernet.' 'LAN address' ([System.Windows.Forms.MessageBoxIcon]::Warning) } })

# Settings tab
Add-Label $tabSettings 'BAR data folder' 25 34 180 | Out-Null
$script:tbDataDir = Add-TextBox $tabSettings 'tbDataDir' $script:initialDataDir 25 61 665
$btnBrowseData = Add-Button $tabSettings 'Browse…' 705 59 110 32
Add-Label $tabSettings 'Engine folder (contains spring.exe)' 25 112 280 | Out-Null
$script:tbEngineDir = Add-TextBox $tabSettings 'tbEngineDir' $script:initialEngineDir 25 139 665
$btnBrowseEngine = Add-Button $tabSettings 'Browse…' 705 137 110 32
$settingsHelp = Add-Label $tabSettings 'Defaults match your working batch files. If BAR updates to another engine version, select the new engine folder here.' 25 186 780 42
$settingsHelp.ForeColor = [System.Drawing.Color]::FromArgb(80, 90, 104)
$btnSaveSettings = Add-Button $tabSettings 'Save settings' 25 245 150 38
$btnDetectEngine = Add-Button $tabSettings 'Detect engine' 190 245 150 38
$btnOpenData = Add-Button $tabSettings 'Open data folder' 355 245 160 38
$btnCheckSetup = Add-Button $tabSettings 'Check setup' 530 245 130 38
$btnBrowseData.Add_Click({ $d = New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description = 'Select the Beyond All Reason data folder'; $d.SelectedPath = $script:tbDataDir.Text; if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $script:tbDataDir.Text = $d.SelectedPath } })
$btnBrowseEngine.Add_Click({ $d = New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description = 'Select the engine folder that contains spring.exe'; $d.SelectedPath = $script:tbEngineDir.Text; if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $script:tbEngineDir.Text = $d.SelectedPath } })
$btnSaveSettings.Add_Click({ try { Save-AppSettings; Show-Message "Settings saved to:`r`n$script:settingsPath" } catch { Show-UiError $_ } })
$btnDetectEngine.Add_Click({
    try {
        $data = $script:tbDataDir.Text.Trim(); $root = Join-Path $data 'engine'
        $engines = @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction Stop | ForEach-Object { $p = Join-Path $_.FullName 'spring.exe'; if (Test-Path -LiteralPath $p) { Get-Item -LiteralPath $p } } | Sort-Object LastWriteTime -Descending)
        if (-not $engines.Count) { throw "No spring.exe was found under:`r`n$root" }
        $script:tbEngineDir.Text = Split-Path -Parent $engines[0].FullName
        Set-Status "Detected engine: $($script:tbEngineDir.Text)"
    } catch { Show-UiError $_ }
})
$btnOpenData.Add_Click({ try { $p = $script:tbDataDir.Text.Trim(); if (-not (Test-Path -LiteralPath $p)) { throw "Folder does not exist: $p" }; Start-Process explorer.exe -ArgumentList "`"$p`"" } catch { Show-UiError $_ } })
$btnCheckSetup.Add_Click({
    try {
        $exe = Get-EngineExe
        $data = $script:tbDataDir.Text.Trim()
        $modPath = Join-Path $data 'games\BAR.sdd\modinfo.lua'
        $msg = "Engine found:`r`n$exe`r`n`r`nData folder:`r`n$data"
        if (Test-Path -LiteralPath $modPath) { $msg += "`r`n`r`nAwesome mod folder check: BAR.sdd found." } else { $msg += "`r`n`r`nNOTE: games\BAR.sdd\modinfo.lua was not found at the default path. If your custom game uses another folder name, this is only a warning." }
        if (Test-Path -LiteralPath (Join-Path $data 'maps')) { $msg += "`r`nMaps folder exists." }
        Set-Status 'Setup check complete.'
        Show-Message $msg 'Setup check'
    } catch { Show-UiError $_ }
})

$script:statusLabel = New-Object System.Windows.Forms.Label
$script:statusLabel.Location = New-Object System.Drawing.Point(20, 600); $script:statusLabel.Size = New-Object System.Drawing.Size(860, 24)
$script:statusLabel.Text = 'Ready. Host and friend must have matching BAR engine, Awesome mod, and map files.'
$script:statusLabel.ForeColor = [System.Drawing.Color]::FromArgb(55, 72, 91)
$script:form.Controls.Add($script:statusLabel)
$footer = New-Object System.Windows.Forms.Label
$footer.Location = New-Object System.Drawing.Point(20, 625); $footer.Size = New-Object System.Drawing.Size(860, 18)
$footer.Text = 'Direct LAN launcher only — no online server, chat, matchmaking, or automatic mod distribution.'
$footer.ForeColor = [System.Drawing.Color]::FromArgb(105, 113, 124)
$footer.Font = New-Object System.Drawing.Font('Segoe UI', 8)
$script:form.Controls.Add($footer)

# Ensure the default engine selection is usable if the exact older directory is gone.
if (-not (Test-Path -LiteralPath (Join-Path $script:initialEngineDir 'spring.exe'))) {
    try {
        $root = Join-Path $script:initialDataDir 'engine'
        $found = @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue | ForEach-Object { $p = Join-Path $_.FullName 'spring.exe'; if (Test-Path -LiteralPath $p) { Get-Item $p } } | Sort-Object LastWriteTime -Descending)
        if ($found.Count -gt 0) { $script:tbEngineDir.Text = Split-Path -Parent $found[0].FullName }
    } catch { }
}

$script:form.Add_Shown({
    if (-not (Import-MapCache)) {
        try { Update-MapListFromGame } catch { Set-Status ('Map list not loaded: ' + $_.Exception.Message + ' You can still type the map title.') }
    } else { Set-Status 'Map list loaded from cache. Click Refresh map list after installing new maps.' }
    & $script:updateMapPreview
})

[void]$script:form.ShowDialog()
