# Persistência PostgreSQL do Caixa

## Infraestrutura
Render PostgreSQL gratuito `caixa-by-vagasio-db`, Ohio, PostgreSQL 18, database `caixa_vagasio`, ativo. O Render informou expiração em 16/12/2026. O plano Free tem 1 GB, expira em 30 dias e não oferece backups/PITR; atualizar o plano ou migrar/exportar antes do vencimento. O usuário instruiu usar Free agora e atualizar depois.

## Aplicação
- Backend usa psycopg2-binary com `DATABASE_URL`; exige TLS em destino Render.
- `CAIXA_REQUIRE_POSTGRES=true` impede fallback silencioso para SQLite.
- Signup exige PostgreSQL ativo, `CAIXA_DEMO_MODE=false` e `CAIXA_PUBLIC_SIGNUP=true`; bootstrap continua protegido por segredo.
- Serviço `srv-dbtgqk95efls73cleqig`, Free e branch `development-v1`, conectado à URL interna do banco Ohio. Segredo de conexão nunca entra no repo/log/resposta.
- PostgreSQL é base de desenvolvimento compartilhada. Não há backup/PITR no plano Free; usuário deverá atualizar antes da expiração. Não inserir dados críticos até backup/recuperação serem configurados.

## Testes e operação confirmada
- Primeira CI PG `43960213644` identificou SQL date/text, upsert e expectativa desatualizada de cadastro; correções commitadas em `b0459941fe9b00124c6edb653ece012130346114`.
- CI PostgreSQL `43961405960` passou; CI geral `43961406033` passou.
- Deploy de integração live `dep-dbtngvt6e07s73akjpfg`, commit `b0459941...`. `/health` reporta `storage=postgresql`.
- Cadastro de conta fictícia testado HTTP 201; login HTTP 200; `/v1/me` devolveu titular e tenant esperados. Depois de outro redeploy, login continuou funcionando. Nenhuma conta real de terceiros usada.
- Cadastro público habilitado só após guard PostgreSQL verdadeiro e modo demo desligado. Endpoint de demo retorna 404 agora.
- Suite SQLite local do próximo ajuste UI: 51 passed, 1 skipped (teste exige CI PostgreSQL), 1 aviso Starlette/httpx. O próximo ajuste de UI ainda não está commitado nem publicado.

## Próximos itens
- Publicar o rótulo dinâmico do ambiente para a interface deixar de chamar o serviço de demonstração.
- Validar visual da tela de login/cadastro após atualizar.
- Criar alerta/calendário de vencimento antes de 16/12/2026 e plano aprovado para migração/backup. Banco Free não equivale a armazenamento definitivo de produção com recuperação.
- Payments e fiscal seguem desativados. Render web service é Free e pode dormir por inatividade; não é produção comercial homologada.