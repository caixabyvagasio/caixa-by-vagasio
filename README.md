# Caixa by VagasIO

Plataforma SaaS de gestão comercial, vendas e PDV multissegmento, inicialmente desenvolvida em repositório independente e privado. O domínio e a hospedagem definitiva serão decididos depois.

## Estado atual: V1 de desenvolvimento

O repositório já contém uma **base funcional local para varejo geral**, com API e interface web: autenticação inicial por empresa, usuários e perfis, cadastro de produtos, ajustes auditáveis de estoque, abertura/movimentação/fechamento de caixa e vendas de teste exclusivamente em dinheiro, com troco e proteção contra duplicidade.

O protótipo inicial permanece em `index.html`; a aplicação funcional está em `app/` e `frontend/`. Consulte o [guia da base V1](docs/caixa-v1/README.md) para executar, conhecer limites e ler próximos marcos. A concepção multissegmento original está em `docs/concepcao-funcional.md`.

## Importante

**Não é um sistema de produção e não deve operar com clientes ou dinheiro real.** O banco atual é SQLite local, ainda não há hospedagem/deploy, backups, recuperação de conta, revisão de segurança, pagamentos eletrônicos, integração fiscal, multifilial ou operação offline. A interface demonstra somente as funções que já conversam com a API; recursos ausentes não são prometidos como disponíveis.

Não adicionar credenciais ou segredos a arquivos versionados. Provisionamento inicial e segredos são configurados externamente no ambiente local.
