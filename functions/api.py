import sys
import os

# Add parent directory to path to import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
from werkzeug.wrappers import Response
import json

def handler(event, context):
    """
    Netlify serverless function handler for Flask app
    """
    # Parse the incoming request
    path = event.get('path', '/')
    httpMethod = event.get('httpMethod', 'GET')
    headers = event.get('headers', {})
    queryStringParameters = event.get('queryStringParameters', {})
    body = event.get('body', '')
    
    # Build WSGI environ
    environ = {
        'REQUEST_METHOD': httpMethod,
        'SCRIPT_NAME': '',
        'PATH_INFO': path,
        'QUERY_STRING': '&'.join([f"{k}={v}" for k, v in queryStringParameters.items()]) if queryStringParameters else '',
        'CONTENT_TYPE': headers.get('content-type', ''),
        'CONTENT_LENGTH': str(len(body)) if body else '0',
        'SERVER_NAME': headers.get('host', 'localhost').split(':')[0],
        'SERVER_PORT': '443',
        'SERVER_PROTOCOL': 'HTTP/1.1',
        'wsgi.version': (1, 0),
        'wsgi.url_scheme': 'https',
        'wsgi.input': None,
        'wsgi.errors': sys.stderr,
        'wsgi.multithread': False,
        'wsgi.multiprocess': True,
        'wsgi.run_once': False,
    }
    
    # Add headers to environ
    for key, value in headers.items():
        key = key.upper().replace('-', '_')
        if key not in ('CONTENT_TYPE', 'CONTENT_LENGTH'):
            environ[f'HTTP_{key}'] = value
    
    # Create response
    response_data = {'statusCode': 200, 'headers': {}, 'body': ''}
    
    def start_response(status, response_headers, exc_info=None):
        response_data['statusCode'] = int(status.split()[0])
        for header, value in response_headers:
            response_data['headers'][header] = value
    
    # Handle the request
    try:
        with app.request_context(environ):
            from flask import request as flask_request
            
            # Add body data
            if body:
                environ['wsgi.input'] = body
            
            response = app.full_dispatch_request()
            
            if isinstance(response, Response):
                response_data['statusCode'] = response.status_code
                response_data['headers'] = dict(response.headers)
                response_data['body'] = response.get_data(as_text=True)
            else:
                response_data['body'] = str(response)
    
    except Exception as e:
        response_data['statusCode'] = 500
        response_data['body'] = json.dumps({'error': str(e)})
        response_data['headers']['Content-Type'] = 'application/json'
    
    return response_data
