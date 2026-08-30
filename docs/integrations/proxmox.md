# Integração Proxmox (API nativa)

## Pré-requisitos

- Proxmox VE acessível pela URL configurada pelo operador.
- **API Token dedicado** — nunca senha de root.

## Criação do token (no host Proxmox)

```bash
# 1. Usuário dedicado, sem login interativo
pveum user add nyra-observer@pve --comment "NYRA homelab integration"

# 2. Role mínima de leitura (auditoria) + start de VM se desejado
pveum role add NYRAObserver -privs "Datastore.Audit,Sys.Audit,VM.Audit"
# Opcional (permite proxmox_vm_start): acrescente VM.PowerMgmt
# pveum role modify NYRAObserver -privs "Datastore.Audit,Sys.Audit,VM.Audit,VM.PowerMgmt"

pveum aclmod / -user nyra-observer@pve -role NYRAObserver

# 3. Token sem separar privilégios (herda os do usuário)
pveum user token add nyra-observer@pve nyra --privsep 0
```

Guarde o `token ID` (`nyra-observer@pve!nyra`) e o `secret` exibido **uma única vez**.

## Configuração (.env)

```env
NYRA_PROXMOX_ENABLED=true
NYRA_PROXMOX_URL=https://proxmox.example.invalid:8006
NYRA_PROXMOX_TOKEN_ID=nyra-observer@pve!nyra   # não é secret por si só
NYRA_PROXMOX_TOKEN_SECRET=<SECRET>
NYRA_PROXMOX_VERIFY_SSL=true
```

## TLS / self-signed

- `NYRA_PROXMOX_VERIFY_SSL=true` é obrigatório antes de qualquer envio do API Token.
- Para certificado interno/self-signed, instale a CA emissora no repositório de confiança do Windows do usuário que executa a NYRA.
- `NYRA_PROXMOX_TLS_FINGERPRINT` pode adicionar pin SHA-256 da leaf cert (hex, sem separadores), mas não substitui a validação da cadeia TLS. Divergência aborta com `PROXMOX_TLS_FINGERPRINT_MISMATCH`.
- Configuração com `verify_ssl=false` é rejeitada; nenhuma chamada autenticada é iniciada nesse estado.

## Privilégios por endpoint

| Operação | Privilégio necessário |
|---|---|
| `/version`, `/nodes`, node status, cluster status | `Sys.Audit` |
| `cluster/resources` (VMs/LXC/storage) | `Datastore.Audit` + `VM.Audit` |
| Guest status/config | `VM.Audit` |
| Tasks (leitura) | `Sys.Audit` |
| `start/shutdown/stop/reboot/reset` | `VM.PowerMgmt` |

## Teste rápido

```powershell
# após configurar o .env
python scripts/homelab-smoke.py --only proxmox
```

Ou manual:

```bash
curl -k -H "Authorization: PVEAPIToken=USER@REALM!TOKENID=REDACTED" https://proxmox.example.invalid:8006/api2/json/version
```

## Tools expostas

Leitura: `proxmox_node_status`, `proxmox_list_vms`, `proxmox_vm_status` (aceita vmid **ou nome**), `proxmox_storage_status`, `proxmox_cluster_status`, `proxmox_recent_tasks`.

Ações (com task grounding): `proxmox_vm_start` → aguarda UPID até `stopped`/`OK` → reconsulta guest → só então reporta com `effect_verified`. Toda mutação exige opt-in global e approval de uso único conforme `docs/homelab-control-plane.md`.

Destruição (`qm destroy`, snapshots, storage delete) **não está implementada** nesta fase.

## Erros normalizados

`PROXMOX_AUTH_MISSING` · `PROXMOX_AUTH_FAILED` · `PROXMOX_PERMISSION_DENIED` · `PROXMOX_API_UNAVAILABLE` · `PROXMOX_API_ERROR` · `PROXMOX_API_INVALID_RESPONSE` · `PROXMOX_TASK_FAILED` · `PROXMOX_VM_NOT_FOUND` · `PROXMOX_TLS_FINGERPRINT_MISMATCH`


## V11 — Configuração pela UI + Credential Broker (prompt11_1)

- Formulário completo: Enabled/Base URL/API Token ID+Secret/TLS Verification/
  Preferred Node/Request Timeout. Ações Save · Test Connection · Enable ·
  Disable · Disconnect · Diagnostics · Open.
- Token ID/Secret vivem só no Credential Broker (`proxmox_api_token_id`,
  `proxmox_api_token_secret`), com migração silenciosa das settings legadas;
  frontend recebe apenas `token_*_configured: true|false`.
- Persistência não-secreta em `data/proxmox-config.json` + runtime settings;
  restaurada após restart (`apply_to_runtime` no boot).
- Estados: DISABLED · UNCONFIGURED (sem token) · AUTH_FAILED (401) · READY
  (teste autenticado) · DEGRADED · OFFLINE · TLS_ERROR. Nunca `READY` sem API
  token validado.
- Endpoints: `GET|PUT /api/proxmox/config`, `POST /api/proxmox/test`,
  `POST /api/proxmox/disconnect`, `GET /api/proxmox/inventory` (nodes, QEMU e
  LXC separados, storage) e `POST /api/homelab/proxmox/guests/{ref}/action`
  (power ops reutilizam o executor do Control Plane; Stop permanece
  DESTRUCTIVE na política — mais restritivo que ELEVATED).


## V11.2 — Hotfix de consistência de estados (prompt11_2)

- **Fonte única de `enabled`** (`load_config`): precedência
  `data/proxmox-config.json` → runtime overlay (`data/settings-v33.json`) →
  settings legadas (.env). `public_status`, `test_connection`, Integration
  Center e formulário usam EXATAMENTE essa resolução: é impossível a UI
  mostrar `Habilitada: Sim` enquanto o teste responde `PROXMOX_DISABLED`.
- Sem token com `enabled=true`: estado `UNCONFIGURED` e teste
  `PROXMOX_UNCONFIGURED` — nunca `DISABLED`. Integração realmente
  desabilitada: `DISABLED`/`PROXMOX_DISABLED` coerentes em toda superfície.
- Habilitar/Desabilitar (Integration Center) grava na fonte autoritativa
  (`set_enabled`) além do runtime overlay — sobrevive a restart.
- Save flow com merge por chave: toggle de enabled não apaga URL/timeout;
  `PUT /api/proxmox/config` usa `exclude_unset`.
- TLS: certificado self-signed com verificação ON vira `PROXMOX_TLS_ERROR`
  (nunca `AUTH_FAILED`); a verificação nunca é desligada automaticamente.
- UI: card Proxmox com Configurar/Abrir (view interna; UNCONFIGURED mostra
  aviso honesto em vez de cards falsos), Salvar/Testar conexão/Cancelar,
  resumo READY somente com dados reais (version/nodes/QEMU/LXC/storage).
