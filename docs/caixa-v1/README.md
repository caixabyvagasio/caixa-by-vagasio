# Caixa by VagasIO — base funcional V1 (desenvolvimento)

Repositório independente do Caixa. O domínio próprio e a hospedagem de produção serão definidos na etapa de migração. O protótipo inicial foi substituído por uma base funcional local da primeira etapa de varejo geral.

## O que está implementado

- API FastAPI e interface web responsiva servidas pelo mesmo aplicativo.
- Provisionamento inicial protegido por chave bootstrap; senha PBKDF2 e sessão Bearer assinada por segredo configurável.
- Isolamento de usuários, catálogo, vendas, caixa, movimentos de estoque e auditoria por empresa.
- Criação e desativação de usuários com perfis titular, administrador, gerente, operador e estoque; autorização verificada no servidor.
- Produtos com SKU, unidade, preço, custo e saldo; entrada/baixa de estoque com justificativa, registro de autor e proteção contra saldo negativo.
- Um caixa aberto por empresa: fundo inicial, suprimento, sangria, venda, valor esperado e conferência/fechamento.
- Venda de teste em dinheiro, cálculo de troco, decremento transacional do estoque, histórico e chave de idempotência contra requisição repetida.
- Dashboard inicial e telas de acesso, produtos/estoque, frente de caixa, vendas e equipe.
- Testes automatizados cobrindo as operações principais e isolamento multiempresa.

## Limitações — não usar para operação real

Este é um marco de desenvolvimento local, não um serviço de produção. O banco inicial SQLite é para testes em uma única instância e armazenamento persistente local; ainda é necessário migrar para PostgreSQL antes de hospedar, com migrações, transações concorrentes e isolamento validados. Não há implantação, domínio, backups, monitoramento nem revisão independente de segurança.

Ainda não existem pagamentos eletrônicos/Pix, emissão fiscal, integração de periféricos, multifilial, cadastro empresarial/CNPJ, assistente de implantação, catálogo de módulos/planos, cancelamento/devolução autorizado, compras/fornecedores, exportações ou operação offline. O único meio de pagamento suportado é dinheiro recebido no caixa. O bootstrap cria uma empresa por vez, com chave de provisionamento fornecida externamente.

Não insira dados reais, credenciais ou dados de clientes. Não configure segredos em arquivos versionados. Não exponha publicamente este ambiente.

## Rodar localmente

Requer Python 3.11 ou superior. Instale as dependências em um ambiente virtual:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export CAIXA_TOKEN_SECRET="um-segredo-local-longo-e-aleatorio"
export CAIXA_BOOTSTRAP_SECRET="outra-chave-local-longa-e-aleatoria"
export CAIXA_DB_PATH="./data/caixa.sqlite3"
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Abra `http://127.0.0.1:8000`. A primeira empresa deve ser provisionada pela API documentada em `http://127.0.0.1:8000/docs`, usando `POST /v1/bootstrap` e enviando o cabeçalho `X-Bootstrap-Key` igual ao valor da variável local `CAIXA_BOOTSTRAP_SECRET`. O corpo exige razão/nome, slug, responsável, e-mail e senha com pelo menos 12 caracteres. Depois entre na tela com o slug, e-mail e senha. Não use essas chaves de exemplo ou qualquer segredo publicado em produção.

## Testes

```sh
PYTHONPATH=. pytest -q
```

Os testes usam banco temporário e contas fictícias; não alteram nem acessam dados reais.

## Próximos marcos antes de qualquer publicação

1. Substituir SQLite por PostgreSQL e validar migrações e operações concorrentes.
2. Desenhar deployment/ambientes, gestão segura do primeiro titular, recuperação de acesso, rate limit, backups e observabilidade.
3. Implementar entidades empresa/filial, papéis/permissões detalhados, catálogos por módulos/plano e onboarding.
4. Completar fluxo de varejo: clientes, compras, cancelamentos/devoluções com autorização e relatórios.
5. Fazer revisão de segurança e testes autorizativos multi-tenant; validar operação visual em desktop/celular.
6. Integrar pagamentos/fiscal apenas após provedor, homologação, regras legais e consentimentos definidos.
7. Só depois escolher hospedagem, publicar ambiente de homologação com dados fictícios e migrar domínio quando você decidir.
