import os,sys,tempfile,unittest
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
import core
class TenantTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=os.path.join(self.tmp.name,'test.db')
        core.initialize(self.path)
        self.a=core.add_tenant('Loja_A_bot',101,'Loja A',self.path)
        self.b=core.add_tenant('Loja_B_bot',202,'Loja B',self.path)
    def tearDown(self): self.tmp.cleanup()
    def test_isolation(self):
        pid=core.add_product(self.a,101,'A', '19,90',3,self.path)
        self.assertEqual(len(core.products(self.a,self.path)),1)
        self.assertEqual(len(core.products(self.b,self.path)),0)
        with self.assertRaises(PermissionError): core.edit_product(self.a,202,pid,'title','Invadido',self.path)
        with self.assertRaises(LookupError): core.edit_product(self.b,202,pid,'title','Invadido',self.path)
        self.assertEqual(core.products(self.a,self.path)[0]['title'],'A')
    def test_owner(self):
        with self.assertRaises(PermissionError): core.update_shop(self.a,202,'title','Falso',self.path)
        core.update_shop(self.a,101,'title','Novo',self.path)
        self.assertEqual(core.tenant(self.a,self.path)['title'],'Novo')
        self.assertEqual(core.tenant(self.b,self.path)['title'],'Loja B')
    def test_price(self):
        self.assertEqual(core.parse_price('19,90'),1990)
        self.assertEqual(core.parse_price('0,01'),1)
        for bad in ('-1','NaN','inf','1000001'):
            with self.assertRaises(ValueError): core.parse_price(bad)
    def test_stock_and_deactivate(self):
        pid=core.add_product(self.a,101,'Produto','5',2,self.path)
        core.edit_product(self.a,101,pid,'stock_count',0,self.path)
        self.assertEqual(core.products(self.a,self.path)[0]['stock_count'],0)
        core.edit_product(self.a,101,pid,'active',0,self.path)
        self.assertEqual(len(core.products(self.a,self.path)),0)
if __name__=='__main__': unittest.main()
