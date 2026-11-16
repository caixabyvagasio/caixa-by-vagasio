# PostgreSQL do Caixa by VagasIO

## Infraestrutura atual
- Render PostgreSQL `caixa-by-vagasio-db`, Ohio, PG18, plano Free, database `caixa_vagasio`; criado em 2026-11-16, expiração reportada pelo Render em 2026-12-16 20:59 UTC.
- Serviço web `caixa-by-vagasio-dev` permanece em Free; recebe tráfego público, pode suspender após inatividade.
- `DATABASE_URL` interna está configurada via variável protegida do Render e não foi exposta em logs/código. `CAIXA_REQUIRE_POSTGRES=true` faz falhar o startup em vez de voltar silenciosamente ao SQLite. `CAIXA_DEMO_MODE=false`; `CAIXA_PUBLIC_SIGNUP=true`. Signup endpoint aplica guard PostgreSQL e modo demo.
- Deploy live `dep-dbtnkpm7bikc73f690m0`, commit `ac5248ee7cf212a62b5f086c5069c7a82e62da26` (rótulos UI adaptativos). Deploy inicial com PostgreSQL `dep-dbtngvt6e07s73akjpfg`, commit `b0459941fe9b00124c6edb653ece012130346114`.
- `/health` verificado `storage=postgresql`, `demo_enabled=false`, `signup_enabled=true`; pagamentos e fiscal permanecem desativados.

## Validação que foi executada
- Primeira CI real Postgres `43960213644` falhou com divergências SQL específicas (date/text, upsert perfil e expectativa incorreta de signup). Corrigidas no commit `b0459941fe9b00124c6edb653ece012130346114`.
- CI PostgreSQL `43961405960` success e CI normal `43961406033` success no mesmo commit.
- Signup de empresas fictícias foi exercitado: HTTP 201; login HTTP 200; `/v1/me` identificou titular correto. Uma empresa de smoke autenticou novamente após redeploy do serviço, mostrando persistência no PostgreSQL.
- Smoke visual/browser: a navegação carregou conta autenticada de teste; lista “Equipe e acessos” mostrou só o titular daquele tenant. Teste independente API criou outro tenant fictício e conferiu que GET users contém só o próprio e-mail, sem vazamento cross-tenant.
- Depois, commit de textos/indicador de ambiente `ac5248ee7cf212a62b5f086c5069c7a82e62da26`: CI geral `43963765448` success, CI PostgreSQL `43963765433` success. Deploy live verificado.
- Suite local desse ajuste: 51 passed, 1 skipped por fixture exclusiva de Postgres CI, 1 aviso de depreciação Starlette/httpx. Referências estáticas DOM completas e formulários balanceados; validação browser autenticada confirmou a tela de equipe.

## Limite essencial do plano Free
O Free Postgres Render expira após 30 dias, tem 1 GB e não oferece backups/PITR. O usuário instruiu usar esse plano como base agora e atualizar depois. Para manter acesso/continuidade, será necessário atualizar/migrar antes de 16/12/2026 e executar/exportar backup; não usar Free como garantia de armazenamento permanente ou dado crítico sem backup recuperável. Conta e senhas de smoke foram somente sintéticas; não foram incluídas aqui.

## Produto e segurança
Pagamentos eletrônicos, fiscal e homologação continuam desligados. Signup público está ativo para criar empresas isoladas neste ambiente de desenvolvimento e fica tenant-scoped. A demonstração da interface é escondida quando `/health` reporta demo desligada; aviso agora mostra PostgreSQL e a necessidade de atualização do plano. Em produção comercial ainda faltam revisão de segurança, backup/restore validado, recuperação de conta, LGPD operacional, rate limit externo, fiscal/pagamentos e outros módulos.