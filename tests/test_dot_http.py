import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'alex-mcp'/'app'))
from dot_http import IssuerVerifier,create_app,_authenticated_app,SCOPE

class HTTPTests(unittest.TestCase):
    def test_production_factory_rejects_missing_or_unsafe_config(self):
        for issuer in ['', 'http://issuer.example', 'https://user:password@issuer.example']:
            with self.assertRaises(ValueError):
                create_app(None,issuer=issuer,jwks_url='https://issuer.example/keys',resource='https://alex.example/mcp/',owner_subject='owner')
        with self.assertRaises(ValueError):
            create_app(None,issuer='https://issuer.example',jwks_url='https://issuer.example/keys',resource='https://alex.example/mcp/',owner_subject='')

    def test_real_jwt_verification_without_network(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        v=IssuerVerifier(issuer='https://issuer.example',jwks_url='https://issuer.example/keys',resource='https://alex.example/mcp/',owner_subject='owner')
        claims={'iss':v.issuer,'aud':v.resource,'sub':'owner','scope':SCOPE,'client_id':'chatgpt','iat':int(time.time()),'exp':int(time.time())+60}
        with patch.object(v.keys,'get_signing_key_from_jwt',return_value=SimpleNamespace(key=key.public_key())):
            token=jwt.encode(claims,key,algorithm='RS256')
            self.assertEqual(asyncio.run(v.verify_token(token)).subject,'owner')
            for change in [{'iss':'https://evil.example'},{'aud':'other'},{'sub':'wife'},{'scope':'read'},{'exp':1}]:
                self.assertIsNone(asyncio.run(v.verify_token(jwt.encode({**claims,**change},key,algorithm='RS256'))))
            self.assertIsNone(asyncio.run(v.verify_token(jwt.encode(claims,'not-rsa-key-material-for-security-test-only',algorithm='HS256'))))
            self.assertIsNone(asyncio.run(v.verify_token('invalid')))

    def test_private_tunnel_requires_owner_file_token_and_loopback(self):
        import tempfile
        import os
        from starlette.testclient import TestClient
        from dot_http import create_tunnel_app
        class Bridge:
            principal='owner'
            def authorize(self,principal):
                if principal!='owner':raise PermissionError()
        with tempfile.TemporaryDirectory() as directory:
            path=os.path.join(directory,'token')
            with open(path,'w') as f:f.write('test-only-not-live-'+'x'*48)
            os.chmod(path,0o644)
            with self.assertRaises(ValueError):create_tunnel_app(Bridge(),credential_path=path,owner_subject='owner')
            os.chmod(path,0o600)
            app=create_tunnel_app(Bridge(),credential_path=path,owner_subject='owner')
            body={'jsonrpc':'2.0','id':1,'method':'server/discover'}
            headers={'X-Alex-Bridge-Key':'test-only-not-live-'+'x'*48}
            with TestClient(app,client=('127.0.0.1',1234)) as c:
                self.assertEqual(c.post('/mcp/',json=body).status_code,401)
                self.assertEqual(c.post('/mcp/',json=body,headers=headers).status_code,200)
                self.assertEqual(c.get('/.well-known/oauth-protected-resource').status_code,403)
            with TestClient(app,client=('203.0.113.10',1234)) as c:
                self.assertEqual(c.post('/mcp/',json=body,headers=headers).status_code,403)

    def test_http_auth_scope_audience_and_body(self):
        from mcp.server.auth.provider import AccessToken
        from starlette.testclient import TestClient
        class Bridge:
            def authorize(self,principal):
                if principal!='owner':raise PermissionError()
        class Verifier:
            async def verify_token(self,token):
                if token=='bad':return None
                return AccessToken(token=token,client_id='chatgpt',subject='owner',expires_at=int(time.time())+60,
                    scopes=[] if token=='scope' else [SCOPE],resource='other' if token=='audience' else 'https://alex.example/mcp/',claims={'iss':'https://issuer.example'})
        app=_authenticated_app(Bridge(),Verifier(),issuer='https://issuer.example',resource='https://alex.example/mcp/',owner_subject='owner')
        with TestClient(app) as c:
            self.assertEqual(c.get('/.well-known/oauth-protected-resource').status_code,200)
            body={'jsonrpc':'2.0','id':1,'method':'server/discover'}
            self.assertEqual(c.post('/mcp/',json=body).status_code,401)
            self.assertEqual(c.post('/mcp/',json=body,headers={'Authorization':'Bearer bad'}).status_code,401)
            for token in ['scope','audience']:
                self.assertEqual(c.post('/mcp/',json=body,headers={'Authorization':'Bearer '+token}).status_code,403)
            headers={'Authorization':'Bearer good'}
            r=c.post('/mcp/',json=body,headers=headers)
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.json()['result']['supportedVersions'],['2026-07-28'])
            self.assertEqual(c.post('/mcp/',content='not-json',headers=headers).status_code,415)
            self.assertEqual(c.post('/mcp/',content='x'*262145,headers={**headers,'Content-Type':'application/json'}).status_code,413)
            self.assertEqual(c.post('/mcp/',json=[],headers=headers).status_code,400)

if __name__=='__main__':unittest.main()
