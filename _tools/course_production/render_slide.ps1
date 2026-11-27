param(
    [Parameter(Mandatory)] [string] $Source,
    [Parameter(Mandatory)] [int] $Page,
    [Parameter(Mandatory)] [string] $Output
)

$ErrorActionPreference = "Stop"
$app = New-Object -ComObject PowerPoint.Application
$book = $null
try {
    $book = $app.Presentations.Open($Source, -1, 0, 0)
    if ($Page -lt 1 -or $Page -gt $book.Slides.Count) {
        throw "Slide number outside presentation: $Page"
    }
    $width = 1600
    $height = [int][Math]::Round($width * $book.PageSetup.SlideHeight / $book.PageSetup.SlideWidth)
    $book.Slides.Item($Page).Export($Output, "PNG", $width, $height)
} finally {
    if ($null -ne $book) {
        $book.Close()
    }
}
