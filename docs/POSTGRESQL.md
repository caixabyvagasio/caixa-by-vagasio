# Persistência PostgreSQL do Caixa

## Estado
Integração inicial PostgreSQL em implementação. Render Free `caixa-by-vagasio-db`, região Ohio, PostgreSQL 18, database `caixa_vagasio`, ID interno registrado com segurança em Render. Plano gratuito criado em 16/11/2026, expira em 16/12/2026. Ao expirar, os dados ficam inacessíveis e após grace period podem ser apagados. Plano Free sem backups automáticos/PITR. É uma base compartilhada para desenvolvimento/teste até usuário atualizar o plano; exportar antes da expiração e atualizar o plano/destino em tempo.

## Aplicação
`DATABASE_URL` é consumida pelo backend psycopg2-binary com TLS obrigatório em host Render; `CAIXA_REQUIRE_POSTGRES=true` impede startup se a URL estiver ausente ou o driver faltar. Enquanto `DATABASE_URL` não for configurada no Render, a aplicação permanece no banco anterior; deploy PostgreSQL deve ser em uma única alteração controlada de env var com requirements, CI aprovado e health smoke verificado.

O adapter mantém testes SQLite e traduz as consultas existentes; migrations schema versionadas 1–4 seguem na startup. É necessária CI PostgreSQL real antes de conectar. O signup continua desligado em demo e fica permitido somente com PostgreSQL ativo e flag pública autorizada. `/v1/bootstrap` permanece fechado por segredo e só deve ser usado após configurar o serviço persistente.

## Migração operacional planejada
- DB gratuito criado vazio: não há dados da instância efêmera a copiar; cadastros perdidos do SQLite não são recuperáveis por esta integração.
- Mudar o serviço para `DATABASE_URL` interno do banco só depois do CI PostgreSQL aprovado; preservar os demais envs, marcar `CAIXA_REQUIRE_POSTGRES=true`, `CAIXA_DEMO_MODE=false`, `CAIXA_PUBLIC_SIGNUP=true` na alteração, e deploy commit correspondente.
- Smoke: health reporta `storage=postgresql`; criar uma empresa fictícia por signup, logar, verificar `/v1/me`; repetir consulta e restart/redeploy controlado do serviço e confirmar tenant ainda existe antes de dar caminho a empresas reais.
- Após smoke, fornecer/confirmar fluxo e login com user sem capturar senha real.
- Não incluir DATABASE_URL/password em logs, documentação, arquivos ou Github.

## Testes e limitações
Suite SQLite permanece para desenvolvimento. Workflow `postgres.yml` inicia PostgreSQL 16 temporário no GitHub Actions e executa a mesma suite isolada por banco por teste. Sem aprovação desse CI e sem smoke Render, ainda não considerar aplicação conectada/pronta. Free database é temporário com vencimento; falta backup/restore validado para declarar uso definitivo/dados críticos. O usuário pretende atualizar plano antes da expiração.