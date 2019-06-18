import boto3
import uuid
import os
import logging
from json import loads, dumps, JSONEncoder
from decimal import Decimal
from botocore.exceptions import ClientError
from functools import reduce

logger = logging.getLogger()
logger.setLevel(logging.INFO)

db_table_client = boto3.resource('dynamodb')
db_client = boto3.client('dynamodb')
s3_client = boto3.client('s3')


# Helper class to convert a DynamoDB item to JSON.
class DecimalEncoder(JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            if o % 1 > 0:
                return float(o)
            else:
                return int(o)
        return super(DecimalEncoder, self).default(o)


def _marshall(value):
    if isinstance(value, str):
        return {"S": value}
    elif isinstance(value, bool):
        return {"BOOL": value}
    elif isinstance(value, float) or isinstance(value, int):
        return {"N": str(value)}
    elif value is None:
        return {"NULL": True}
    elif isinstance(value, list):
        return {"L": [_marshall(v) for v in value]}
    elif isinstance(value, dict):
        return {"M": {k1: _marshall(v1) for k1, v1 in value.items()}}
    else:
        raise Exception("Don't know how to marshall type: " + str(type(value)))


def marshall(value):
    """
    Serialize  Python dict to DynamoDB object.
    :param value:
    :return:
    """
    return {k: _marshall(v) for k, v in value.items()}


def _unmarshall(value):
    if "S" in value:
        return str(value["S"])
    elif "BOOL" in value:
        return value["BOOL"]
    elif "N" in value:
        return float(value["N"]) if "." in value["N"] else int(value["N"])
    elif "NULL" in value:
        return None
    elif "L" in value:
        return [_unmarshall(v) for v in value["L"]]
    elif "M" in value:
        return {k: _unmarshall(v) for k, v in value["M"].items()}
    else:
        raise Exception("No known data type descriptor found. Maybe this is already JSON?", value)


def unmarshall(value):
    """
    Serialize DynmoDb object to Python dict.
    :param value:
    :return:
    """
    return {k: _unmarshall(v) for k, v in value.items()}


def event_handler(event, context):
    """
         lambda function
        :param event:
        :param context:
        :return:
    """

    def response(message, status_code):
        return {
            'statusCode': str(status_code),
            'body': dumps(message),
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
        }

    def split_row_to_chunks(row_tray, output_tray):
        len_tray = len(row_tray)
        if len_tray < 100:
            size = len_tray
        else:
            size = len_tray // 100
        splitsize = 1.0 / size * len_tray

        for i in range(size):
            # output_tray.append({'refId': str(uuid.uuid4()),
            #                     'timeSeries': row_tray[int(round(i * splitsize)):int(round((i + 1) * splitsize))]})
            output_tray.append(marshall({'refId': str(uuid.uuid4()),
                                         'timeSeries': row_tray[
                                                       int(round(i * splitsize)):int(round((i + 1) * splitsize))]}))
        return output_tray

    def find_match(data, condition, location=False):
        if location:
            coordinates = data['last_known_position']['geometry']['coordinates']
            return coordinates[0] >= condition[0] and coordinates[1] <= condition[1]

    def getShipsLastSeenTropics(ships_tray):
        matches = [1 if find_match(ship, (-23.5, 23.5), location=True) else 0 for ship in ships_tray]
        return reduce(lambda x, y: x + y, matches)

    def get_item(key_obj):
        """
            Retrieve obj from DynamoDb using the PK
            :param key_obj:
            :return:
        """
        table_name = os.environ.get('TABLE_NAME')
        table = db_table_client.Table(table_name)
        try:
            response = table.get_item(Key=key_obj)
        except ClientError as e:
            logger.info(e.response['Error']['Message'])
        else:
            logger.info("GetItem succeeded:")
            return response['Item']

    def load_db(data):
        """
            load data from file.
            :param data:
            :return:
        """

        table_name = os.environ.get('TABLE_NAME')
        # table = db_client.Table(table_name)

        logger.info('#### Writing data to DynamoDb ####')
        # with table.batch_writer() as batch:
        #     for datum in data:
        #         batch.put_item(Item=datum)
        for datum in data:
            db_client.put_item(TableName=table_name, Item=datum)
        logger.info('#### Writing completed ####')

    def json_parser(filereader):
        """
            parse json and normalize
            :param: stream obj
            :return: json
        """
        result = []
        # json_rows = loads(filereader, parse_float=Decimal)
        json_rows = loads(filereader)
        for row in json_rows:
            split_row_to_chunks(row['data'], result)
        return result

    logger.info('## ENVIRONMENT VARIABLES')
    logger.info(os.environ)
    logger.info('## EVENT')
    logger.info(event)

    if event.get('Records') is not None and event['Records'][0]['eventSource'] == 'aws:s3':
        bucket = event['Records'][0]['s3']['bucket']['name']
        filename = event['Records'][0]['s3']['object']['key']
        json_obj = s3_client.get_object(Bucket=bucket, Key=filename)
        jsonFileReader = json_obj['Body'].read()
        normalized_rows = json_parser(jsonFileReader)

        logger.info('Loading data to DynamoDB...')

        load_db(normalized_rows)

        logger.info('Data loaded successfully to DynamoDB')

    if event.get('httpMethod') is not None and event['httpMethod'] == 'POST' and event['path'] == '/ships':
        key_obj = {'refId': loads(event['body'])['refId']}
        try:
            record = get_item(key_obj)
            logger.info(f'{record}')

            logger.info('##### Processing the number of ships last seen in the Tropics ######')
            num_ships_tropics = getShipsLastSeenTropics(record['timeSeries'])
            return response({'num_ships_tropics': num_ships_tropics}, 200)
        except Exception as e:
            logger.error(e)
            return response({'message': 'Server Error'}, 500)
