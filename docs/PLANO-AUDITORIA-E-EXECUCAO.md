# Caixa by VagasIO — Auditoria e plano contínuo

Atualização inicial: 2026-11-16. Base auditada: `development-v1`, commit `ff72d7a2a3606eec2cdcd188b3a2ad313db17955`. Deploy atual é uma demonstração Render Free, não produção; esta ordem proíbe publicação em produção sem nova autorização.

## Inventário auditado

Stack: FastAPI/Python, SQLite, HTML/CSS/JavaScript sem framework, pytest/TestClient, deploy Render Free. Repositório contém `app/main.py` (API/esquema/migrações inline), `frontend/index.html`, `app.js`, `app.css`, workflows CI e documentação. 39 testes do commit publicado passaram localmente; CI GitHub success. Navegador confirmou página, catálogo/relatório de estoque e health HTTP 200. Auditoria deste turno é leitura de código + suíte automatizada + smoke checks, não teste E2E completo de cada botão/fluxo.

### Funcionalidades existentes e evidência
- Autenticação bearer HMAC, PBKDF2 310k, papéis owner/admin/manager/operator/stock, rate-limit local: `app/main.py`.
- Organizações por tenant e filtros de companhia em CRUD e transações principais; cobertura parcial de isolamento no pytest.
- Perfil de segmento, questionário, sugestões de módulos e versões append-only. Não há entitlements, habilitação real, assinatura/plano nem menus derivados dos direitos.
- Produtos/categorias/código de barras/estoque auditado; compras/recebimentos idempotentes; clientes; caixa/vendas somente dinheiro; cancelamento/estorno; contas a pagar com baixa manual; relatório de vendas e avaliação estimada de estoque.
- No seed de demonstração: dados fictícios de varejo, visíveis e mutáveis a todos que acessam a demo.

## Achados classificados

| ID | Severidade | Causa e impacto | Correção e aceitação |
|---|---|---|---|
| AUD-01 | Crítica | DB configurado em `/tmp/caixa.sqlite3` no Render Free; perde dados, sem backup/restore. Inadequado a qualquer operação real. | Manter demo explicitamente descartável; implementar PostgreSQL/migrations/backup/restore em infraestrutura autorizada. Testar migração, restore e consistência. A dependência de provedor/destino durável bloqueia ambiente comercial. |
| AUD-02 | Alta | `CAIXA_PUBLIC_SIGNUP=true` no serviço demo permite criar tenants públicos no SQLite temporário. | Bloquear signup quando `DEMO_MODE` estiver ligado; cobrir com teste. Verificar posteriormente configuração de deploy sem substituir secrets/variáveis inadvertidamente. |
| AUD-03 | Alta | GET clientes não aplicava RBAC; listagem incluía tax_id, notas, telefone e e-mail para qualquer papel autenticado; detalhe/histórico igualmente sem RBAC. | Restringir detalhe a owner/admin/manager; listagem de operador apenas nome/ID necessário à venda, busca restrita a nome; negar stock. Testes de PII e tenant. |
| AUD-04 | Alta | Limiter usa memória de processo; perde estado ao reiniciar e não coordena instâncias. | Manter proteção local, mas exigir rate limit no edge/distribuído antes de uso público. Testar limites; infra externa pendente. |
| AUD-05 | Alta | Sem Portal Master, multi-filial, RBAC por filial, planos/entitlements, suporte ou administração global. | Projetar identidade Master separada e isolamento cross-tenant; implementar em fase, testes negativos antes das telas. |
| AUD-06 | Alta | PDV tem apenas dinheiro; sem Pix/cartão, pagamento misto, devolução efetiva ou comprovante fiscal. | Criar ledger e contratos de provedores, estados pending/authorized/failed/refunded; só sandbox e credenciais fornecidas, sem simular aprovação. |
| AUD-07 | Alta | Persistência/financeiro são parciais: contas a pagar simples, recebíveis/parcelamento, conciliação, despesas, DRE e centros de custo ausentes. | Modelo de ledger idempotente e trilha, receber/pagar parcial e reconciliação, sem misturar marcação manual com confirmação externa. |
| AUD-08 | Média/Alta | Segmentos sugerem recursos, mas o backend chama todas as features `available=1`; módulos especializados não existem nem têm entitlement. | Estado separado recomendado/implementado/contratado/ativo, ativação server-side e configuração sem apagar histórico. |
| AUD-09 | Média | Clientes retornam dados sensíveis; motivo central é autorização ausente e resposta excessiva. | Corrigido nesta primeira etapa (ver progresso abaixo); prosseguir revisão de todos os endpoints por papel e minimização. |
| AUD-10 | Média | Frontend usa prompts/alerts do navegador para movimentos, recebimentos, baixas e histórico de cliente; UX móvel/teclado/acessibilidade limitada. | Substituir em etapas por diálogos acessíveis, estados ocupados/erro/vazio, validação inline, testes funcionais. |
| AUD-11 | Média | Migrações são idempotentes inline no startup SQLite, sem versão formal, backend de banco alternativo ou ensaios de rollback/restore. | Criar sistema versionado e testado antes de PostgreSQL; só migrar dados após backup verificável e autorização operacional. |
| AUD-12 | Baixa/Média | 39 testes API focam happy/negative paths; não há suíte browser E2E automatizada, segurança independente, accessibility audit nem carga. | Ampliar unit/integration/E2E, RBAC matriz, concorrência, scripts de restore, axe/lighthouse quando ferramentas disponíveis. |

## Plano por fases, prioridade e critérios de aceitação

1. **Auditoria e registro (em andamento):** mapa de módulos/rotas, riscos, plano versionado e execução rastreável. Aceite: inventário contra código/CI/runtime e gaps explicitados.
2. **Críticos: privacidade, demo e estabilidade:** corrigir RBAC/Pii, bloquear signup na demo, teste de isolamento; checar concorrência/idempotência e segurança de respostas. Aceite: testes por papel/tenant e CI.
3. **Persistência/migrações:** abstraction para PostgreSQL sem perder SQLite demo, migration runner versionado, backup/restore testado, health de DB e documented env. Aceite: suite em SQLite + PostgreSQL; não usar dado comercial antes de backup restore e ambiente autorizado.
4. **Modelo SaaS seguro:** organização/filial, claims/entitlements, papéis/ações, sessões e audit; API Master isolada e logs. Aceite: nenhuma rota privilegiada acessível por tenant normal; teste cross-tenant.
5. **Portal Master:** empresas, filiais, planos/entitlements, módulos, terminais, suporte, comunicados e indicadores reais. Aceite: cada ação persiste, audita, pagina, filtra e tem autorização server-side.
6. **Portal Empresarial:** categoria/variante/inventário, cotação/pedido, troca/devolução, contas a pagar/receber e relatórios. Aceite: operações transacionais/ledger, estado/auditoria e validação ponta a ponta.
7. **PDV:** operadores/terminal, scanner wedge, descontos, vendedor, múltiplos pagamentos por interface provider, suspender/recuperar, comprovante, devolução e fechamento. Aceite: idempotência/concorrência e nenhuma aprovação fictícia.
8. **Planos e segmentos:** wizard da empresa, escolha de plano e revisão, entitlements reais e menu/dash por módulo. Aceite: só ativa recurso implementado+contratado; desligar não elimina histórico.
9. **Módulos especializados:** restaurante, supermercados, vestuário, serviços/beleza e construção integrados ao núcleo. Aceite por módulo com fluxos reais, testes e privacidade; não usar mero catálogo como conclusão.
10. **Fiscal/dispositivos/offline:** contratos/adaptadores, sandbox, contingência, auditoria de autorização e fila idempotente. Aceite: certificações e acesso externo necessários; jamais anunciar produção sem homologar.
11. **Preparação de homologação:** pentest, accessibility/mobile/E2E/carga, backup/restore, runbooks, monitoramento, documentação, release e rollback. Aceite: CI e validação operacional completos; deploy produtivo exige autorização expressa.

## Progresso deste ciclo
- [x] Auditar commit atual, ambiente, endpoints, esquemas, papeis, dependências, testes e implantação.
- [x] Reproduzir localmente os 39 testes do commit (`39 passed`, 1 warning conhecido de depreciação TestClient/httpx).
- [ ] Corrigir signup público na demonstração e exposição de dados de clientes; adicionar testes.
- [ ] Commitar correções e plano apenas na branch `development-v1`; CI.
- [ ] Prosseguir para migrações/persistência, SaaS/Master, PDV e módulos conforme fases; registrar bloqueios de infraestrutura e homologação.

## Restrições / dependências

Não provisionar banco pago, disco, backups, provedor de pagamento, gateway fiscal, certificados, domínio ou device sem autorização específica/custo aprovado. Não transferir nem apagar dados de produção. O Render atual é demo descartável e não é produção nem destino de dados reais. Código local/branch de desenvolvimento não equivale a feature pronta/publicada/homologada.
