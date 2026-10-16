# Nexo Bot Store — MVP 0.1 (ambiente separado)

Primeira versão **funcional, mas limitada** de um motor multi-loja. Não é o bot comercial pronto para vender.

## Arquitetura

- **Código compartilhado** (`core.py` e `telegram_bot.py`) entre instalações.
- **Identidade por tenant**: cada loja tem `tenant_id`, proprietário, username do bot, produtos e configurações próprios.
- **Isolamento lógico**: toda operação de produto usa `tenant_id`; escrita administrativa exige o proprietário correspondente.
- **Um processo de polling por bot** neste MVP; para escalar, migrar para orquestrador/webhooks e PostgreSQL com políticas de isolamento, filas e observabilidade.
- **Tokens separados**: cada processo lê `TENANT_BOT_TOKEN` de variável de ambiente, sem gravar tokens na base.
- **Pagamentos desabilitados**: não há Pix, carteira, pedidos ou credenciais do Mercado Pago neste MVP. Integração futura requer autorização individual, validação de webhook, idempotência e conciliação.
- **Nexo Store oficial intocada**: não importar banco nem copiar tokens do bot oficial.

## Executar localmente com bot de testes

1. Crie um **novo bot de testes** no BotFather, separado do bot oficial.
2. `python -m pip install -r requirements.txt`
3. `python provision.py --username NovoBotTeste --owner-id 123456789 --title "Minha Loja"`
4. Configure `TENANT_BOT_TOKEN` **fora do código** e rode `python telegram_bot.py --tenant 1`.
5. No Telegram, use `/start`, `/produtos`, `/admin`, `/editar`.

Para executar vários bots, crie um tenant para cada um e execute processos separados, cada qual com token secreto e ID correspondente. **Não reutilize o mesmo token em processos simultâneos.** Para persistência em produção, configure volume para `NEXO_MVP_DB`; SQLite em volume único compartilhado entre vários servidores não é solução de escala.

## Segurança e limitações

- A versão inicial exige provisionamento por CLI; o comprador ainda não consegue criar bot sozinho.
- Painel Telegram: editar nome, boas-vindas, suporte; cadastrar produto e editar nome, preço, quantidade ou desativar.
- Não inclui checkout, entrega automática, licenciamento, backups automáticos, recuperação de abandono, admins secundários ou sistema de assinaturas. Estes serão módulos seguintes.
- **Não use para vendas reais** nesta fase. Antes de comercializar: migração para PostgreSQL, testes de concorrência, gestão de segredos, backups/restauração, auditoria, consentimento e privacidade de dados, fluxo de pagamento isolado por tenant e testes completos.

## Testes

`python -m unittest discover -s tests -v`
