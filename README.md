# Caixa by VagasIO

Repositório do Caixa by VagasIO, plataforma multissegmento de gestão comercial e PDV. A primeira versão prioriza varejo geral.

## Ambiente publicado

A demonstração está em https://caixa-by-vagasio-dev.onrender.com/. Entre pelo botão **Acessar demonstração fictícia**. É uma conta compartilhada com catálogo demonstrativo; os dados podem ser apagados ou modificados por outros visitantes.

**Não use dados reais nem para operação comercial.** É uma versão inicial de avaliação: usa SQLite em armazenamento efêmero, sem garantia de persistência, backups ou suporte de produção; não há Pix/cartão, integração fiscal, recuperação de acesso, multifilial nem operação offline. O serviço gratuito pode adormecer ou reiniciar.

## V1 implementada

API FastAPI e interface web; login/isolamento de empresa, perfis e permissões server-side, produtos, movimentos de estoque auditáveis, caixa e venda em dinheiro com troco e idempotência. A demonstração usa empresa e catálogo fictícios e não cria contas de clientes.

Consulte a [concepção funcional](docs/concepcao-funcional.md) e [detalhes e próximos marcos](docs/caixa-v1/README.md).

## Desenvolvimento e testes

Requer Python 3.11+. Instale `requirements.txt`, configure `CAIXA_TOKEN_SECRET` e `CAIXA_BOOTSTRAP_SECRET` fortes (cada um com pelo menos 32 bytes) e execute `uvicorn app.main:app --host 127.0.0.1 --port 8000`. Testes: `PYTHONPATH=. pytest -q`.

`CAIXA_DEMO_MODE` é opt-in e deve ser habilitado apenas em ambiente isolado de demonstração. Nunca grave segredos no repositório.

## Antes de uma versão comercial

Substituir SQLite por PostgreSQL e confirmar custos/consentimento; backups e restauração; rate limits e revisão de segurança; migrações; monitoramento; recuperação de acesso; política de privacidade e fluxos de consentimento; testes de carga, autorização e isolamento; homologação de pagamentos e fiscal antes de anunciar essas integrações. Nenhum dado comercial deve ser inserido na demonstração pública.
