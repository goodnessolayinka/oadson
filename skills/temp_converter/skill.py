async def run(payload: dict) -> dict:
    args = payload.get('args', {})
    value = args.get('value')
    target = str(args.get('target_unit', '')).lower()

    if value is None or target not in ['celsius', 'fahrenheit']:
        return {'status': 'error', 'message': 'Invalid input. Provide a numeric value and target_unit (celsius or fahrenheit).'}

    try:
        if target == 'celsius':
            result = (value - 32) * 5 / 9
            unit_label = '°C'
        else:
            result = (value * 9 / 5) + 32
            unit_label = '°F'
        
        return {
            'status': 'success',
            'result': f'{round(result, 2)}{unit_label}',
            'value': round(result, 2),
            'unit': unit_label
        }
    except Exception as e:
        return {'status': 'error', 'message': str(e)}