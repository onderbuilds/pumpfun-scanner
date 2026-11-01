TOKEN_PROGRAMS=("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA","TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ALLOWED_EXT={'metadataPointer','tokenMetadata'}
def check_account(a):

    if not a: return dict(ok=False,reason='account_not_found')
    try: p=a["data"]["parsed"]["info"]; ext=[x.get("extension") for x in p.get("extensions",[])]
    except Exception: return dict(ok=False,reason='unparsed')
    bad=[]
    if a["owner"] not in TOKEN_PROGRAMS: bad.append('unknown_token_program')
    if p.get("mintAuthority"): bad.append('mint_authority_set')
    if p.get("freezeAuthority"): bad.append('freeze_authority_set')
    if set(ext)-ALLOWED_EXT: bad.append('ext:'+','.join(sorted(set(ext)-ALLOWED_EXT)))
    sup=int(p.get("supply","0"))/10**p.get("decimals",6)
    if sup>1.001e9: bad.append(f'nonstandard_supply_{sup:.0f}')
    return dict(ok=not bad,reason=';'.join(bad),program=a["owner"][:8],ext=ext,supply=sup)
