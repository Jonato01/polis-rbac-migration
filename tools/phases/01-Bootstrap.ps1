Set-StrictMode -Version Latest

function Invoke-Phase01 {
    <# Controlli su az: presente, loggato, RG raggiungibile, ruolo Owner/UAA dell'operatore. #>
    param([Parameter(Mandatory)][hashtable]$Context)
    if (-not (Test-CommandAvailable 'az')) { Stop-Migration 'Azure CLI (az) non trovata nel PATH: il bootstrap deve installarla prima' }
    $acct = Invoke-Az @('account', 'show') -AllowFailure
    if (-not $acct) { Stop-Migration 'Nessun login Azure attivo (az login)' }
    $Context.Sub = $acct.id
    Write-Step "  Subscription: $($acct.name) ($($acct.id)) utente: $($acct.user.name)"
    $grp = Invoke-Az @('group', 'show', '-n', $Context.Rg) -AllowFailure
    if (-not $grp) { Stop-Migration "Resource group '$($Context.Rg)' non trovato nella subscription corrente" }
    $Context.RgId = $grp.id
    $Context.Location = $grp.location
    if (-not (Test-OperatorRole -Scope $grp.id)) {
        Stop-Migration "L'utente loggato non ha Owner o User Access Administrator su $($Context.Rg): impossibile assegnare ruoli alle managed identity"
    }
    Write-Step '  Ruolo operatore: ok (Owner / User Access Administrator)'
}
