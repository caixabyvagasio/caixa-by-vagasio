# Progresso de execução — auditoria e primeira correção

Data: 2026-11-16. Branch alvo `development-v1`; HEAD auditado inicialmente `ff72d7a2a3606eec2cdcd188b3a2ad313db17955`.

## Concluído nesta retomada
- Auditoria de código/árvore: 25 blobs, FastAPI + SQLite + JS/CSS, 1,445 linhas Python de API, suite de integração inicial 39 testes.
- Estado de deploy verificado: Render Free, branch `development-v1`, autodeploy off, SQLite em `/tmp/caixa.sqlite3`, demo habilitada, cadastro público habilitado. Não é produção nem armazenamento durável.
- Reprodução dos testes do HEAD: 39 passed; smoke público: homepage e health HTTP 200; UI de estoque mostrou 3 produtos fictícios, valores estimados. Não foram criadas nem alteradas operações de negócio.
- Criado `docs/PLANO-AUDITORIA-E-EXECUCAO.md` com inventário, problemas classificados, causas, impactos, correções e critérios de aceitação.
- Corrigido no código-fonte: `/v1/companies/register` rejeita cadastros sempre que demo está habilitada, ainda que `CAIXA_PUBLIC_SIGNUP=true`.
- Corrigidos endpoints de clientes: papéis `owner/admin/manager` podem consultar campos pessoais e histórico; `operator` só lista/consulta nomes para identificação no PDV e só cadastra nome; papel `stock` não tem leitura dos clientes. Tenant segue aplicado no SQL.
- Testes novos cobrem demo+signup simultaneamente ligados, omissão de e-mail/telefone/documentos/anotações para operadores, leitura de detalhe bloqueada para operador, leitura negada a estoque e criação do cliente pelo operador apenas com nome.
- Suíte após correção: 41 passed, 1 aviso recorrente Starlette TestClient/httpx.

## Não feito / estado de entrega
- Correções ainda não commitadas/publicadas neste checkpoint; preservar ambiente público até CI e decisão de deploy. Nenhuma alteração em produção, secrets ou dados externos.
- PostgreSQL, backups, restore, Master, filiais, entitlements de verdade, finanças completas, pagamento/fiscal, offline, módulos de segmento, auditoria E2E/mobile/segurança seguem pendentes.

## Próximos passos executáveis
1. Verificar diff e segurança dos testes; commit em `development-v1`; confirmar CI.
2. Corrigir os achados de privacidade/demo em eventual deploy somente no Render de desenvolvimento, após validar que branch/serviço permanecem Free e não autodeploy; nunca promover para produção.
3. Abrir fase estrutural de migrations e persistência, implementar interface DB e suporte PostgreSQL com testes de migração/backup/restore, sem contratar provider.
4. Próxima feature funcional isolada que não dependa de infra: ampliar permissionamento/UX/API e construir Portal Master em desenvolvimento com tenant administrativo separado.
5. Atualizar este arquivo a cada incremento e repetir testes. Documentar explicitamente qualquer dependência externa, custos ou aprovação específica exigida.
