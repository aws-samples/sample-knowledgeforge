# Step 1: Install CDK dependencies
cd cdk;
pip install -r requirements.txt

# Step 2: Bootstrap CDK (one-time per account/region)
cdk bootstrap aws://555555555555/eu-west-1

# Step 3: Deploy the CDK stack -> This creates VPC, SQS, DLQ, DynamoDB, ECR, ECS, guardrail, S3 event notification - everything.
 
cdk deploy --context account=555555555555

Review the IAM changes it shows and confirm with y.
 
# Step 4: Build and push the container image
After CDK deploy completes, grab the ECR repo URI from the outputs, then:
 
cd ../pipeline
 
Authenticate Docker to ECR -> 
aws ecr get-login-password --region eu-west-1 | docker login --username AWS --password-stdin 555555555555.dkr.ecr.eu-west-1.amazonaws.com
 
Build the image ->
docker build -t article-pipeline .
 
Tag it ->
docker tag article-pipeline:latest 555555555555.dkr.ecr.eu-west-1.amazonaws.com/article-pipeline:v1
 
Push it ->
docker push 555555555555.dkr.ecr.eu-west-1.amazonaws.com/article-pipeline:v1

# Step 5: Update ECS service to use the image
The task definition currently references the ECR repo without a specific tag. After pushing, force a new deployment:
 
aws ecs update-service \
 --cluster article-pipeline-cluster \
 --service ArticlePipelineStack-PipelineService* \
--force-new-deployment \
 --region eu-west-1

Or you can get the exact service name from -> 
aws ecs list-services --cluster article-pipeline-cluster --region eu-west-1


# Step 6: Verify
Check ECS task is running: aws ecs list-tasks --cluster article-pipeline-cluster --region eu-west-1;

Check CloudWatch Logs: look at the article-pipeline-logs log group for "Pipeline started - polling" message;

Test by uploading a themes.json to the source bucket - it should trigger processing automatically;

One heads-up: if the output bucket article-generation-output-bucket already exists, CDK deploy will fail at Step 4. Delete it first or let me know and I'll change the CDK to import it instead.
 








