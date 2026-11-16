# Persistência PostgreSQL do Caixa

## Infraestrutura
Render Postgres gratuito `caixa-by-vagasio-db`, Ohio, PostgreSQL 18, database `caixa_vagasio`, ativo. O Render informou vencimento em 16/12/2026. Free tem 1 GB, expira em 30 dias e não inclui backups/PITR. Antes do vencimento precisa atualizar o plano ou migrar/exportar. Sem usar dados críticos até estratégia de recuperação ser configurada.

## Aplicação e migração
- Aplicação usa psycopg2-binary quando `DATABASE_URL` tem prefixo PostgreSQL, exige TLS para destinos `*.render.com`; sem URL pode continuar SQLite apenas para testes locais.
- `CAIXA_REQUIRE_POSTGRES=true` faz o serviço falhar ao iniciar sem DATABASE_URL válida/driver, para não cair silenciosamente para SQLite.
- Signup público exige backend PostgreSQL, `CAIXA_DEMO_MODE=false` e `CAIXA_PUBLIC_SIGNUP=true`. Bootstrap continua provisionamento administrativo separado protegido por segredo.
- O ambiente publicado ainda não recebeu a connection string nem deploy deste adapter neste estado. O banco criado permanece vazio; cadastros do SQLite temporário não são recuperáveis.
- Após adaptar integração: conectar com Internal URL (mesma região), ativar require postgres e demo false, deploy; confirmar `/health` mostra `storage=postgresql`, registro fictício, login, `/v1/me`, confirmar após restart do serviço; só então cadastro de empresa será disponibilizado.
- Não imprimir/gravar connection string ou senha em log, repo ou resposta.

## Validação
O adapter mantém testes SQLite e soma workflow `postgres.yml` com PostgreSQL real no GitHub CI, isolando cada teste em database temporário. Primeira execução `43960213644` encontrou 3 classes de divergência (date/text, UPSERT de catálogo/perfil, expectativa equivocada no teste signup); corrigidas localmente. Suite SQLite local atual: 51 passed, 1 skipped (o teste de signup require PG), 1 warning deprecado de Starlette TestClient. CI PostgreSQL precisa novo commit/execução e aprovação antes de ligar Render.

Migradores numerados 1–4 continuam traduzidos no adapter para SQLite e Postgres. Não declarar migração Render, smoke ou cadastro confirmado antes da execução do CI e deploy controlado subsequente.