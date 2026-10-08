<# Copy the same notice tree to the portable distribution and MSI staging area. #>
param(
  [Parameter(Mandatory = $true)] [string] $Root,
  [Parameter(Mandatory = $true)] [string] $Destination
)
$ErrorActionPreference = 'Stop'
$Notices = @(
  'LICENSE-MIT', 'LICENSE-APACHE', 'NOTICE', 'ATTRIBUTION.md', 'SECURITY.md',
  'assets/fonts/OFL-Inter.txt', 'assets/fonts/OFL-JetBrainsMono.txt',
  'assets/icons/LICENSE-lucide.txt', 'assets/dict/LICENSE-SCOWL.txt',
  'assets/app-icon/LICENSE.txt', 'crates/ui-egui/src/i18n/LICENSE-translations.txt',
  'docs/brand/LICENSE-brand.txt'
)
foreach ($Relative in $Notices) {
  $Target = Join-Path $Destination $Relative
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Target) | Out-Null
  # Missing notices fail packaging; never silently ship an incomplete license tree.
  Copy-Item -LiteralPath (Join-Path $Root $Relative) -Destination $Target
}
foreach ($Relative in 'README.md', 'LICENSE', 'COPYRIGHT') {
  $Source = Join-Path $Root $Relative
  if (Test-Path -LiteralPath $Source) { Copy-Item -LiteralPath $Source -Destination $Destination }
}
if ($env:CRAFT_FONTS_DIR) {
  Get-ChildItem -Path (Join-Path $env:CRAFT_FONTS_DIR 'fonts') -Directory -ErrorAction Stop | ForEach-Object {
    $License = Join-Path $_.FullName 'OFL.txt'
    if (Test-Path -LiteralPath $License) {
      Copy-Item -LiteralPath $License -Destination (Join-Path $Destination "OFL-$($_.Name).txt")
    }
  }
}
