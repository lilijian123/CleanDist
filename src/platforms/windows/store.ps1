# 仅查询当前用户的主应用包及开始菜单名称，不执行应用、安装或卸载操作。
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$names = @{}
try {
    foreach ($item in Get-StartApps -ErrorAction Stop) {
        $family = $item.AppID.Split('!')[0]
        if (-not $names.ContainsKey($family)) { $names[$family] = $item.Name }
    }
} catch {
    # 名称映射不可用时仍可显示包名称，不影响主包的只读查询。
}
$result = @(foreach ($package in Get-AppxPackage -PackageTypeFilter Main -ErrorAction Stop) {
    $name = $names[$package.PackageFamilyName]
    if (-not $name) { $name = $package.Name }
    $icon = ''
    try {
        $manifest = Get-AppxPackageManifest -Package $package.PackageFullName -ErrorAction Stop
        $visual = @($manifest.Package.Applications.Application)[0].VisualElements
        $logo = [string]$visual.Square44x44Logo
        if (-not $logo) { $logo = [string]$manifest.Package.Properties.Logo }
        if ($logo -and $logo -notlike 'ms-resource:*') {
            $path = Join-Path $package.InstallLocation $logo
            if (Test-Path -LiteralPath $path -PathType Leaf) { $icon = $path }
            else {
                # 图片可能带有缩放或尺寸限定后缀，优先取列表尺寸的资源。
                $filter = [IO.Path]::GetFileNameWithoutExtension($path) + '.*' + [IO.Path]::GetExtension($path)
                $files = Get-ChildItem -LiteralPath ([IO.Path]::GetDirectoryName($path)) -Filter $filter -File -ErrorAction SilentlyContinue
                $chosen = $files | Sort-Object @{ Expression = { if ($_.Name -match 'targetsize-48') { 0 } else { 1 } } }, Name | Select-Object -First 1
                if ($chosen) { $icon = $chosen.FullName }
            }
        }
    } catch {
        # 包清单或图片不可读取时保留应用记录，由界面显示默认图标。
    }
    [PSCustomObject]@{
        name = [string]$name
        version = [string]$package.Version
        publisher = [string]$package.Publisher
        path = [string]$package.InstallLocation
        icon = [string]$icon
    }
})
ConvertTo-Json -InputObject $result -Depth 3 -Compress
