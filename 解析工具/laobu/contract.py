"""Validate the precise JSON-schema keyword subset used by the V1 contract.

This is intentionally not a general Draft 2020-12 implementation. It allows
offline validation without adding a dependency or downloading a schema.
"""
import json
import math
import re

def validate_contract(instance, root_schema):
    errors = []
    def matches(value, name):
        return {"object":isinstance(value,dict),"array":isinstance(value,list),"string":isinstance(value,str),"null":value is None,"boolean":isinstance(value,bool),"integer":isinstance(value,int) and not isinstance(value,bool),"number":isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)}.get(name,False)
    def equal(a,b):
        return json.dumps(a,sort_keys=True,ensure_ascii=False)==json.dumps(b,sort_keys=True,ensure_ascii=False)
    def walk(value,schema,path):
        if '$ref' in schema:
            ref = root_schema
            for component in schema['$ref'].split('/')[1:]:
                ref = ref[component.replace('~1','/').replace('~0','~')]
            walk(value,ref,path)
        for child in schema.get('allOf',[]):
            walk(value,child,path)
        types = schema.get('type')
        if types and not any(matches(value,t) for t in (types if isinstance(types,list) else [types])):
            errors.append(f'{path}: invalid type, expected {types}')
            return
        if 'const' in schema and not equal(value,schema['const']):
            errors.append(f'{path}: expected constant {schema["const"]}')
        if 'enum' in schema and not any(equal(value,x) for x in schema['enum']):
            errors.append(f'{path}: invalid enum')
        if isinstance(value,dict):
            errors.extend(f'{path}: missing {key}' for key in schema.get('required',[]) if key not in value)
            for key,child in schema.get('properties',{}).items():
                if key in value:
                    walk(value[key],child,f'{path}/{key}')
        if isinstance(value,list):
            if len(value)<schema.get('minItems',0) or len(value)>schema.get('maxItems',math.inf):
                errors.append(f'{path}: invalid item count')
            if 'items' in schema:
                for i,child in enumerate(value):
                    walk(child,schema['items'],f'{path}/{i}')
        if isinstance(value,str) and 'pattern' in schema and not re.search(schema['pattern'],value):
            errors.append(f'{path}: pattern mismatch')
        if isinstance(value,(int,float)) and not isinstance(value,bool):
            if value<schema.get('minimum',-math.inf) or value>schema.get('maximum',math.inf) or 'exclusiveMinimum' in schema and value<=schema['exclusiveMinimum']:
                errors.append(f'{path}: numeric bound violation')
    walk(instance,root_schema,'$')
    return errors
