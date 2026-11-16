# Caixa by VagasIO — V1 de varejo geral

## Funcionalidades

- API FastAPI e interface web adaptável.
- Provisionamento protegido; senhas com PBKDF2 e sessões assinadas.
- Isolamento por empresa, perfis e autorização server-side.
- Produtos, estoque e movimentos com justificativa e trilha de auditoria.
- Abertura/suprimento/sangria/fechamento de caixa; venda em dinheiro, cálculo de troco e idempotência.
- Demonstração pública explicitamente fictícia e compartilhada.
- Branch `production-foundation` contém formulário de criação de empresa self-service (desligado por padrão) e limite inicial de tentativas de login/bootstrap/cadastro.

## Situação de publicação

O serviço público de avaliação funciona em Render Free com SQLite no filesystem efêmero. Não é produção, não garante persistência, backup, disponibilidade ou sigilo dos registros de demonstração. Não cadastrar dados pessoais, preços, estoque, clientes ou dinheiro reais. Não existe integração Pix/cartão, fiscal, periféricos, multifilial ou modo offline.

A branch `production-foundation` é trabalho não implantado. Cadastro está desligado (`CAIXA_PUBLIC_SIGNUP=false`). O limitador atual é por processo e apenas defesa adicional; não substitui rate limiting de borda.

## Preparar para produção

1. Aprovar orçamento de serviço e banco persistente, com preço confirmado no painel antes de criar.
2. Migrar e testar PostgreSQL com transações, migrações e restauração de backup; desativar demo pública.
3. Configurar limite de requisições compartilhado e monitoramento, rotinas de backup/restore, política de sessão e recuperação do titular.
4. Revisar LGPD, segurança, suporte e regras comerciais; testar isolamento e concorrência.
5. Só então criar novo ambiente de produção com segredos novos e domínio escolhido.

Não use este release para operação comercial real.
