# 仅打开已有快捷方式读取目标和图标；不保存、不调用目标，也不执行快捷方式参数。
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$shell = New-Object -ComObject WScript.Shell
$roots = @('StartMenu', 'CommonStartMenu', 'DesktopDirectory', 'CommonDesktopDirectory') |
    ForEach-Object { [Environment]::GetFolderPath($_) } | Where-Object { $_ } | Select-Object -Unique
$result = @(foreach ($root in $roots) {
    foreach ($file in Get-ChildItem -LiteralPath $root -Filter '*.lnk' -Recurse -File -ErrorAction SilentlyContinue) {
        $link = $null
        try {
            $link = $shell.CreateShortcut($file.FullName)
            $target = [Environment]::ExpandEnvironmentVariables($link.TargetPath)
            # 不探测网络目标，不读取远端快捷方式关联的软件。
            if ($target -notmatch '^[A-Za-z]:\\' -or [IO.Path]::GetExtension($target) -ine '.exe') { continue }
            if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { continue }
            $info = [Diagnostics.FileVersionInfo]::GetVersionInfo($target)
            [PSCustomObject]@{
                name = [string]$file.BaseName
                path = [string]$target
                version = [string]$info.ProductVersion
                publisher = [string]$info.CompanyName
                icon = [string]$link.IconLocation
            }
        } catch {
            # 失效或不可访问的单个快捷方式不影响其他入口。
        } finally {
            if ($link) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link) }
        }
    }
})
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
ConvertTo-Json -InputObject $result -Depth 3 -Compress
