# Caixa by VagasIO

Repositório do Caixa by VagasIO, plataforma multissegmento de gestão comercial e PDV. A primeira versão prioriza varejo geral.

## Versão publicada para avaliação

A demonstração está em https://caixa-by-vagasio-dev.onrender.com/. Use **Acessar demonstração fictícia**. A conta de demonstração é compartilhada; dados são inventados, podem ser alterados por visitantes e desaparecer.

**Não use dados reais nem para operação comercial.** O serviço público de avaliação usa plano gratuito e SQLite em armazenamento efêmero: cadastros podem se perder quando o serviço reiniciar, dormir ou for implantado. Sem backups, alta disponibilidade, pagamentos eletrônicos, emissão fiscal ou operação offline.

## V1 funcional desenvolvida

API FastAPI e interface web; usuários e permissões por empresa, produtos, estoque com auditoria, caixa e venda em dinheiro com troco/idempotência. Cadastro self-service e proteção inicial contra tentativas repetidas estão em `production-foundation`, mas o cadastro fica desabilitado por padrão e essa branch não foi implantada.

Consulte a [concepção funcional](docs/concepcao-funcional.md) e o [guia da V1](docs/caixa-v1/README.md).

## Antes de uso real

Ainda é necessário escolher e aprovar infraestrutura com armazenamento durável e backups, endurecer rate limits fora do processo, executar análise independente de segurança, validar recuperação de acesso e LGPD, e homologar fiscal/pagamentos antes de anunciar esses meios. A publicação de demonstração não é autorização de cobrança nem homologação comercial.

## Desenvolvimento da personalização
A branch `development-v1` adiciona catálogo inicial de segmentos, questionários auditáveis e sugestões de módulos por empresa. O catálogo não representa ativação de capacidades ausentes. Consulte `docs/PLANO-EXECUCAO-V1.md` e `docs/STATUS-V1.md` para arquitetura, estado real e bloqueios. Não implantar em produção/homologação sem revisão; não inserir dados reais no Render Free.
