"""One Telegram bot instance per tenant; shared code, tenant-scoped database."""
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
import core

log=logging.getLogger(__name__)

class StoreBot:
    def __init__(self,tenant_id,token):
        self.tenant_id=tenant_id
        self.token=token

    def shop(self):
        return core.tenant(self.tenant_id)

    def is_owner(self,uid):
        return self.shop()['owner_id']==uid

    async def start(self,update:Update,ctx:ContextTypes.DEFAULT_TYPE):
        shop=self.shop()
        if not shop or not shop['enabled']: return await update.effective_message.reply_text('Loja indisponível.')
        core.visit(self.tenant_id,update.effective_user.id)
        await update.effective_message.reply_text(f"🛍️ {shop['title']}\n\n{shop['welcome']}\n\nUse /produtos para ver o catálogo.")

    async def catalog(self,update:Update,ctx:ContextTypes.DEFAULT_TYPE):
        shop=self.shop()
        if not shop or not shop['enabled']: return await update.effective_message.reply_text('Loja indisponível.')
        core.visit(self.tenant_id,update.effective_user.id)
        rows=core.products(self.tenant_id)
        if not rows: return await update.effective_message.reply_text('Ainda não há produtos cadastrados.')
        lines=[f"• #{p['id']} {p['title']} — {core.money(p['price_cents'])} — Estoque: {p['stock_count']}" for p in rows]
        await update.effective_message.reply_text('📦 Produtos:\n'+'\n'.join(lines[:30])+'\n\nCompras e pagamentos ainda não estão habilitados nesta versão de testes.')

    async def admin(self,update:Update,ctx:ContextTypes.DEFAULT_TYPE):
        if not self.is_owner(update.effective_user.id): return await update.effective_message.reply_text('Acesso restrito.')
        buttons=[[InlineKeyboardButton('🎨 Nome da loja',callback_data='admin:title'),InlineKeyboardButton('👋 Boas-vindas',callback_data='admin:welcome')],
                 [InlineKeyboardButton('☎️ Suporte',callback_data='admin:support'),InlineKeyboardButton('📦 Novo produto',callback_data='admin:newproduct')],
                 [InlineKeyboardButton('✏️ Editar produto',callback_data='admin:edithelp')]]
        await update.effective_message.reply_text('⚙️ Painel do proprietário',reply_markup=InlineKeyboardMarkup(buttons))

    async def callback(self,update:Update,ctx:ContextTypes.DEFAULT_TYPE):
        q=update.callback_query
        await q.answer()
        if not self.is_owner(q.from_user.id): return await q.message.reply_text('Acesso restrito.')
        field=q.data.split(':',1)[1]
        if field=='edithelp': return await q.message.reply_text('Use /editar ID campo valor\nCampos: nome, preco, estoque, desativar.\nExemplo: /editar 1 preco 29,90')
        if field not in {'title','welcome','support','newproduct'}: return
        ctx.user_data['pending']=field
        examples={'title':'Digite o novo nome da loja.','welcome':'Digite a mensagem de boas-vindas.','support':'Digite o contato de suporte.','newproduct':'Digite: Nome do produto | Preço | Quantidade\nExemplo: Plano mensal | 19,90 | 5'}
        await q.message.reply_text(examples[field]+'\nUse /cancelar para sair.')

    async def message(self,update:Update,ctx:ContextTypes.DEFAULT_TYPE):
        if not self.is_owner(update.effective_user.id): return
        pending=ctx.user_data.get('pending')
        if not pending: return
        try:
            if pending=='newproduct':
                name,price,stock=[x.strip() for x in update.message.text.split('|')]
                pid=core.add_product(self.tenant_id,update.effective_user.id,name,price,int(stock))
                answer=f'Produto #{pid} cadastrado.'
            else:
                core.update_shop(self.tenant_id,update.effective_user.id,pending,update.message.text)
                answer='Configuração atualizada.'
            ctx.user_data.pop('pending',None)
            await update.message.reply_text('✅ '+answer)
        except (ValueError,PermissionError) as exc:
            await update.message.reply_text(f'⚠️ {exc}. Tente novamente ou /cancelar.')

    async def edit(self,update:Update,ctx:ContextTypes.DEFAULT_TYPE):
        if not self.is_owner(update.effective_user.id): return await update.message.reply_text('Acesso restrito.')
        try:
            pid=int(ctx.args[0]); field=ctx.args[1].lower(); value=' '.join(ctx.args[2:])
            mapping={'nome':'title','preco':'price_cents','estoque':'stock_count','desativar':'active'}
            if field not in mapping: raise ValueError('Campo desconhecido')
            core.edit_product(self.tenant_id,update.effective_user.id,pid,mapping[field],'0' if field=='desativar' else value)
            await update.message.reply_text('✅ Produto atualizado.')
        except (IndexError,ValueError,LookupError) as exc:
            await update.message.reply_text(f'⚠️ Uso: /editar ID nome|preco|estoque|desativar VALOR\n{exc}')

    async def cancel(self,update:Update,ctx:ContextTypes.DEFAULT_TYPE):
        ctx.user_data.pop('pending',None)
        await update.message.reply_text('Operação cancelada.')

    def application(self):
        app=Application.builder().token(self.token).build()
        app.add_handler(CommandHandler('start',self.start))
        app.add_handler(CommandHandler('produtos',self.catalog))
        app.add_handler(CommandHandler('admin',self.admin))
        app.add_handler(CommandHandler('editar',self.edit))
        app.add_handler(CommandHandler('cancelar',self.cancel))
        app.add_handler(CallbackQueryHandler(self.callback,pattern=r'^admin:'))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,self.message))
        return app

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--tenant',type=int,required=True)
    args=parser.parse_args()
    token=os.environ.get('TENANT_BOT_TOKEN','')
    if not token: parser.error('Configure TENANT_BOT_TOKEN no ambiente, nunca no código')
    core.initialize()
    StoreBot(args.tenant,token).application().run_polling(drop_pending_updates=False)
