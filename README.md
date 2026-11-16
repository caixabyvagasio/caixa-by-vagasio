# Caixa by VagasIO

Repositório do Caixa by VagasIO, plataforma multissegmento de gestão comercial e PDV. A primeira versão prioriza varejo geral.

## Ambiente de desenvolvimento

A URL `https://caixa-by-vagasio-dev.onrender.com/` é uma demonstração pública em serviço web gratuito: pode dormir por inatividade e demorar a despertar. A conta demo é compartilhada e fictícia.

O PostgreSQL gratuito do Render foi criado e a adaptação/testes da aplicação estão em andamento; **a versão publicada ainda não foi conectada a ele**. O banco Free vence em 30 dias e não tem backups/PITR; deve ser atualizado/migrado antes do vencimento. Até validar integração, reinicialização e login, use apenas dados fictícios.

A demonstração não tem pagamentos eletrônicos, emissão fiscal ou operação offline e não é homologação comercial.

## V1 funcional desenvolvida

API FastAPI e interface web; usuários e permissões por empresa, produtos, estoque com auditoria, caixa e venda em dinheiro com troco/idempotência. Cadastro self-service fica disponível apenas quando backend PostgreSQL persistente estiver ativo, demo estiver desligada e flag explícita habilitada; bootstrap administrativo permanece protegido por segredo.

Consulte [concepção funcional](docs/concepcao-funcional.md), [guia da V1](docs/caixa-v1/README.md), [plano de execução](docs/PLANO-EXECUCAO-V1.md), [estado da V1](docs/STATUS-V1.md) e [persistência PostgreSQL](docs/POSTGRESQL.md).

## Antes de uso comercial

Validar o CI PostgreSQL e o smoke/restart no Render, backup/export e recuperação, endurecer rate limits fora do processo, executar revisão de segurança, validar recuperação de conta e LGPD e homologar fiscal/pagamentos. Não declarar pagamentos, emissão fiscal, disponibilidade ou recuperação sem integração e validação operacional.

O plano Free Postgres é temporário (30 dias), limitado e sem backup/PITR; a atualização de plano/destino precisa ocorrer antes do vencimento para preservar a continuidade dos dados. A publicação do código não é homologação nem autorização de produção.

## Desenvolvimento da personalização

A branch `development-v1` adiciona catálogo inicial de segmentos, questionários auditáveis e sugestões de módulos por empresa. O catálogo não representa ativação de capacidades ausentes.