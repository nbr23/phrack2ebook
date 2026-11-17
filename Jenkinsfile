pipeline {
    agent any

    options {
        disableConcurrentBuilds()
    }

    stages {
        stage('Checkout'){
            steps {
                checkout scm
            }
        }
        stage('Test') {
            agent {
                docker {
                    image 'ghcr.io/astral-sh/uv:python3.13-bookworm-slim'
                    reuseNode true
                }
            }
            environment {
                UV_CACHE_DIR = '/tmp/uv-cache'
            }
            steps {
                sh 'uv run --no-project --with-requirements phrack2ebook.py --with pytest -- pytest -q -p no:cacheprovider --junitxml=test-results.xml'
            }
            post {
                always {
                    junit 'test-results.xml'
                }
            }
        }
        stage('Prep buildx') {
            steps {
                script {
                    env.BUILDX_BUILDER = getBuildxBuilder();
                }
            }
        }
        stage('Sync github repo') {
            when { branch 'master' }
            steps {
                ghSync()
            }
        }
    }
}
