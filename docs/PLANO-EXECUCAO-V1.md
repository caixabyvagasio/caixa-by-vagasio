# Plano de execução e estado — Caixa by VagasIO

Atualizado em 2026-11-16. Escopo deste ciclo: máximo funcional em homologação gratuita, sem criar despesas nem modificar Certus/VagasIO.

## Decisões arquiteturais
- Preservar FastAPI + SQLite + frontend HTML/CSS/JS do repositório; um monólito modular é mais seguro e econômico nesta fase do que reescrever para React/Nest/PostgreSQL sem infraestrutura aprovada.
- Desenvolver em branch separada `production-foundation`; deploy atual Render Free com autodeploy desligado. Mudanças de produção somente após autorização explícita.
- SQLite atual em `/tmp` é efêmero. Nunca usar com dados reais; PostgreSQL e backups são requisito para produção, bloqueados por opção de custo zero.
- Pagamentos/fiscal não simulam aprovação: somente dinheiro local em demonstração; integrações dependem de adquirente, credenciais, certificação/homologação.
- Cadastros públicos devem ser opt-in, protegidos contra abuso e com termos/consentimentos apropriados antes de operação real.

## Fases, dependências e aceitação
1. Fundação: organização/tenant, autenticação, papéis, dados isolados, auditoria. Aceite: testes de autenticação, criação, permissões e isolamento por tenant.
2. Personalização: catálogo versionado de segmentos, questionários, regras determinísticas, módulos e resumo auditável. Aceite: configuração repetível, dependências e plano respeitados, histórico não apagado.
3. Portal Empresarial: produtos (categoria/código/estoque mínimo), estoque auditável, fornecedores/compras (ordens e recebimento parcial), clientes, vendas, caixa, financeiro gerencial, usuários/filiais e relatórios. Aceite: persistência transacional, trilha de auditoria e limites bem expostos.
4. Terminal PDV: busca/carrinho/atalhos, sessão de caixa, dinheiro, estados de pagamento, idempotência e comprovante. Aceite: venda dupla bloqueada, saldo consistente, nenhuma aprovação de pagamento inventada.
5. Portal Master: catálogo global, organizações/filiais, planos, módulos, dispositivos, suporte, indicadores e auditoria. Aceite: rotas protegidas por perfil Master; sem receita/indicadores fictícios mostrados como reais.
6. Segmentos: implementar primeiro varejo geral; preparar restaurante, supermercado, vestuário, serviços e construção por regras, sem ativar telas não existentes.
7. Integrações: contratos/adaptadores e testes sandbox; ativação depende de credenciais, equipamentos e homologação.
8. Qualidade/operação: migrações, CI, segurança, acessibilidade/responsividade, backups/restore, limites, monitoramento e documentação. Aceite: testes aprovados e ambiente de homologação isolado.

## Ordem de trabalho
Inspeção -> segurança/isolamento -> modelagem e migrações -> catálogo de segmentos -> portal empresarial e PDV -> master -> integrações simuladas claramente rotuladas -> testes e documentação. Cada incremento em branch privada/isolada, CI e validação funcional antes de qualquer promoção.

## Estado verificado de partida
- Repositório público `caixabyvagasio/caixa-by-vagasio`, base inicial `production-foundation` (`9192b19a`); versão mais nova do trabalho deve ficar na branch de desenvolvimento.
- Render `caixa-by-vagasio-dev`, plano Free, SQLite `/tmp/caixa.sqlite3`, autodeploy off.
- Existe login demo público, cadastro empresarial, usuários, produtos/ajustes de estoque auditados, caixa e venda em dinheiro na base existente.
- Não existem: banco durável, backup, multi-filial, catálogo/assistente de segmentos, master, pagamentos integrados, fiscal, compras, clientes, DRE, offline ou integrações de dispositivos.
- O frontend existente tinha formulários HTML aninhados; corrigidos em `9192b19a`, verificar em tela após deploy.
- No começo deste ciclo CI da branch tinha 14 testes aprovados (commit anterior ao patch de estrutura HTML); será atualizado/testado novamente.

## Riscos e dependências humanas/externalizadas
- Dados persistentes e disponibilidade: exigem serviço e armazenamento pagos ou outro provider; não provisionar sem autorização de custo.
- Produção real: exige backup/restauração, política de privacidade/LGPD, suporte e revisão de segurança.
- Fiscal: regras estaduais/municipais, certificado digital e homologação técnica/legal.
- Pix/cartão/TEF: conta comercial, contrato com adquirente/gateway e credenciais; não armazenar PAN/CVV.
- Periféricos: modelos/protocolos e testes físicos.

## Checkpoint
Continuar na branch de desenvolvimento a partir do commit que corrige onboarding e formulários. Próxima tarefa: versionar modelo de segmento/feature catalog determinístico com endpoints, interface de onboarding e testes isolados; não expor cadastro público na produção sem termos e infraestrutura de persistência adequados.
