"""Private operator CLI. Bot token is configured outside the DB and CLI."""
import argparse
import core
p=argparse.ArgumentParser(description='Criar loja isolada')
p.add_argument('--username',required=True,help='Username do bot criado pelo cliente no BotFather')
p.add_argument('--owner-id',required=True,type=int,help='ID Telegram do proprietário')
p.add_argument('--title',required=True)
a=p.parse_args()
core.initialize()
print('Tenant criado:',core.add_tenant(a.username,a.owner_id,a.title))
print('Configure TENANT_BOT_TOKEN como variável secreta no serviço correspondente.')
