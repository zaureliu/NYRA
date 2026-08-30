# NYRA Homelab Control Plane V1

Camada central para **observar, diagnosticar e controlar** o homelab real como recursos estruturados — não mais como IPs onde se executa comandos soltos.

## Arquitetura

```text
                    USER
                      ↓
                   NYRA LLM
                      ↓
              AGENT CONTROLLER (existente)
                      ↓
            HOMELAB CONTROL PLANE  (backend/app/homelab/)
        ┌─────────────┼──────────────┐
        ↓             ↓              ↓
 Host Registry   Integrations   Network Probe
 (registry local)               (ICMP/TCP/HTTP)
        │
        ├─ Proxmox ....... API nativa (token PVE) + tasks UPID
        ├─ Home Assistant  REST API (long-lived token)
        ├─ OpenWrt ....... Trusted SSH (remote_shell existente)
        ├─ Linux ......... Trusted SSH
        └─ Windows ....... método remoto declarado (ssh/winrm/NYRA Remote Node)

 Ações: ACT → VERIFY → REPORT  (grounding obrigatório)
```

O Control Plane **não é um segundo agente**: o AgentController continua decidindo; as tools homelab são capabilities com schema Pydantic, risco, approval, lock e grounding.

## Módulos

| Arquivo | Responsabilidade |
|---|---|
| `backend/app/homelab/models.py` | `HostDefinition`, estados normalizados (`ONLINE`, `OFFLINE`, `DEGRADED`, `UNREACHABLE`, `AUTHENTICATION_FAILED`, `INTEGRATION_UNAVAILABLE`, `DISABLED`), probes |
| `backend/app/homelab/registry.py` | Unified Host Registry local, aliases case-insensitive, validação de duplicatas |
| `backend/app/homelab/health.py` | Network Probe Layer (reusa probes do `network_watch`) e agregação de estado — **ping falhando ≠ host offline** |
| `backend/app/homelab/controller.py` | Orquestração: overview concorrente com cache curto, locks por recurso, approval, ACT→VERIFY, eventos com cooldown |
| `backend/app/homelab/history.py` | Histórico operacional em SQLite (`homelab_history`), sem secrets |
| `backend/app/homelab/policies.py` | Risco/aproval por ação (fail-closed; override só restringe) |
| `backend/app/homelab/tools.py` | Registro das tools no ToolRegistry compartilhado |
| `backend/app/integrations/proxmox/client.py` | Cliente PVE API nativo |
| `backend/app/integrations/home_assistant.py` | Cliente REST HA |
| `backend/app/homelab/adapters/` | OpenWrt/Linux/Windows sobre `RemoteShellService` |

## Unified Host Registry

Fonte única: arquivo local definido por `NYRA_HOMELAB_REGISTRY_PATH` (default: `config/homelab_hosts.local.yaml`). O template público é `config/homelab_hosts.example.yaml`.

- Aliases centralizados, case-insensitive (`roteador` → openwrt; `ha` → home_assistant).
- **Nenhuma credencial no registry**: `credentials_profile` aponta para settings (`settings.proxmox_token`, `settings.home_assistant_token`) ou para o Trusted Host Registry SSH local.
- O clone público não define hosts, endereços ou topologia. O operador cria as entradas no arquivo `.local.yaml` ignorado.

### Probe HTTP autenticado

O probe de reachability de hosts com API usa o mesmo token da integração via `credential_resolver` (o token vive só no header da requisição — nunca em logs, resultados ou LLM):

- Home Assistant com token configurado → `GET /api/` autenticado (HTTP 200). Sem isso, cada ciclo de polling geraria um 401 e entradas de "invalid authentication" no log do HA.
- Home Assistant sem token (§103, auth missing ≠ erro) → probe cai para a raiz `/`, que não gera falha de autenticação no alvo.
- Demais integrações (Proxmox etc.) mantêm probe sem credencial; problema real de credencial continua sendo classificado pela integração como `AUTHENTICATION_FAILED`, não como host offline.

## Tools registradas

Read-only: `homelab_overview`, `homelab_list_hosts`, `homelab_host_status`, `proxmox_node_status`, `proxmox_list_vms`, `proxmox_vm_status`, `proxmox_storage_status`, `proxmox_cluster_status`, `proxmox_recent_tasks`, `ha_status`, `ha_list_entities`, `ha_get_state`, `openwrt_status`, `openwrt_interfaces`, `openwrt_wifi_status`, `openwrt_logs`, `host_metrics`, `host_services`.

Ações de homelab ficam desabilitadas por padrão (`NYRA_HOMELAB_MUTATIONS_ENABLED=false`). Após opt-in explícito, toda mutação — inclusive `proxmox_vm_start` e chamadas de serviço do Home Assistant — exige approval de uso único vinculado ao recurso e à ação.

- Toda mutação retorna `effect_verified` após reconsultar o recurso.
- Approval usa o mesmo `ShellApprovalGate` único (`APPROVAL_REQUIRED` + `approval_id`; o run do Agent pausa em WAITING_APPROVAL).
- Locks por recurso (`proxmox:qemu:103`, `ha:<entity>`) evitam ações simultâneas no mesmo alvo.
- Destruição de VM/storage NÃO existe nesta V1.

## Grounding

- Toda observação vira `ToolObservation` no ledger turn-scoped (sem vazamento entre turnos).
- `UPID` recebido ≠ tarefa concluída: o controller espera `status=stopped` + `exitstatus=OK` antes de verificar o guest.
- Service call aceito pelo HA ≠ efeito confirmado: re-leitura do estado define `effect_verified`.
- Falhas de auth nunca são reportadas como "offline" (`PROXMOX_AUTH_FAILED` ≠ `HOST_UNREACHABLE`).

## Eventos

`HOMELAB_HOST_ONLINE/OFFLINE/DEGRADED`, `PROXMOX_VM_CHANGED`, `PROXMOX_TASK_COMPLETED/FAILED`, `HOME_ASSISTANT_ACTION_VERIFIED` — publicados apenas em transição de estado, com cooldown (`NYRA_EVENT_COOLDOWN_SECONDS`). O loop interno roda a cada `NYRA_HOMELAB_POLL_INTERVAL` (≥30s), sem LLM.

## API HTTP

```text
GET  /api/homelab/status                  config + hosts + integrações
GET  /api/homelab/overview?force=true     estado agregado (cache ~5s)
GET  /api/homelab/hosts/{id}              saúde detalhada de um host
GET  /api/homelab/proxmox/vms             VMs/LXC reais
GET  /api/homelab/proxmox/vms/{ref}       status por vmid ou nome
GET  /api/homelab/home-assistant/status   Core/API/version/entities
GET  /api/homelab/history                 histórico operacional
```

## Frontend

Painel enxuto `HomelabPanel` (dashboard/integrações): estado dos 4 hosts com cor por estado, detalhe (versão/uptime/auth), métricas locais e status das integrações.

## Configuração

```env
NYRA_HOMELAB_ENABLED=true
NYRA_HOMELAB_MUTATIONS_ENABLED=false
NYRA_HOMELAB_REGISTRY_PATH=config/homelab_hosts.local.yaml
NYRA_HOMELAB_DEFAULT_TIMEOUT_SECONDS=5
NYRA_HOMELAB_OVERVIEW_CACHE_SECONDS=5
NYRA_HOMELAB_OFFLINE_FAILURE_THRESHOLD=2
NYRA_PROXMOX_ENABLED=true
NYRA_PROXMOX_URL=https://proxmox.example.invalid:8006
NYRA_PROXMOX_VERIFY_SSL=true
NYRA_HOME_ASSISTANT_URL=https://home-assistant.local   # HTTP com Bearer só em loopback
```

Secrets separados (`NYRA_PROXMOX_TOKEN_ID/SECRET`, `NYRA_HOME_ASSISTANT_TOKEN`) — mascarados em `/api/settings` e nunca logados.

## OpenWrt

Reutiliza o Trusted SSH existente (`remote_shell`): mesmos known_hosts, usuário, capabilities, risco, approval e auditoria. Comandos read-only via ubus/ifstatus/logread são classificados READ_ONLY pelo classifier atual. Primeira validação é somente leitura; nenhuma alteração automática no roteador.

## Windows / DC1

Sem método remoto configurado, o DC1 é tratado honestamente como *network-reachable host* (`CAPABILITY_UNAVAILABLE` ao pedir métricas). Nada de WinRM/firewall/TrustedHosts é alterado automaticamente. Métodos futuros: `ssh`, `winrm`, `nyra_remote_node` (campo `metadata.remote_method`).
