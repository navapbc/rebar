pipeline {
    agent any

    triggers {
        // Hourly backstop, mirroring .github/workflows/reconcile-bridge.yml. Off the top
        // of the hour on purpose — :00 is the most contended scheduling slot.
        cron('23 * * * *')
    }

    options {
        disableConcurrentBuilds()
        timeout(time: 60, unit: 'MINUTES')
        timestamps()
    }

    parameters {
        choice(
            name: 'MODE',
            choices: ['live', 'reconcile-check', 'dry-run', 'bootstrap-strict', 'bootstrap-throttle'],
            description: 'Bridge operation profile (reconcile-check maps to preview)'
        )
    }

    environment {
        BRIDGE_BOT_NAME = 'rebar-bridge[bot]'
        BRIDGE_BOT_EMAIL = 'joeoakhart+bot@navapbc.com'
        REBAR_ENV_ID = 'reconciler'
    }

    stages {
        stage('Checkout and mount tickets') {
            steps {
                checkout scm
                sh '''
                    set -eu
                    if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
                        git fetch --unshallow --filter=blob:none origin
                    fi
                    git fetch --filter=blob:none origin '+tickets:refs/remotes/origin/tickets'
                    git worktree remove --force .tickets-tracker 2>/dev/null || true
                    git worktree add -B tickets .tickets-tracker origin/tickets
                    git config merge.ours.driver true
                    git fetch origin '+refs/reconciler/*:refs/reconciler/*' || true
                '''
            }
        }

        stage('Bootstrap') {
            steps {
                sh '''
                    set -eu
                    python -m pip install .
                    if [ -z "${ACLI_VERSION:-}" ] || [ "$ACLI_VERSION" = "latest" ]; then
                        echo "ACLI_VERSION must pin a concrete release" >&2
                        exit 2
                    fi
                    rm -rf .ci-acli
                    mkdir -p .ci-acli
                    curl --fail --silent --show-error --location \
                        "https://acli.atlassian.com/linux/${ACLI_VERSION}/acli_${ACLI_VERSION}_linux_amd64.tar.gz" \
                        --output .ci-acli/acli.tar.gz
                    if [ -n "${ACLI_SHA256:-}" ]; then
                        printf '%s  %s\n' "$ACLI_SHA256" .ci-acli/acli.tar.gz | sha256sum -c --strict
                    else
                        echo "ACLI_SHA256 unset; downloaded acli was not checksum-verified" >&2
                    fi
                    tar xzf .ci-acli/acli.tar.gz -C .ci-acli --strip-components=1
                    rm .ci-acli/acli.tar.gz
                    test -x .ci-acli/acli
                '''
            }
        }

        stage('Run reconcile bridge') {
            steps {
                withCredentials([
                    usernamePassword(
                        credentialsId: 'rebar-jira',
                        usernameVariable: 'JIRA_USER',
                        passwordVariable: 'JIRA_API_TOKEN'
                    ),
                    string(credentialsId: 'rebar-bot-signing-key', variable: 'REBAR_BOT_SIGNING_KEY')
                ]) {
                    sh '''
                        set -eu
                        export PATH="$WORKSPACE/.ci-acli:$PATH"
                        signing_key="$(mktemp)"
                        trap 'rm -f "$signing_key"' EXIT
                        chmod 600 "$signing_key"
                        printf '%s\n' "$REBAR_BOT_SIGNING_KEY" > "$signing_key"
                        export REBAR_IDENTITY_SIGNING_KEY="$signing_key"
                        printf '%s\n' "$JIRA_API_TOKEN" | acli jira auth login \
                            --site "$JIRA_URL" \
                            --email "$JIRA_USER" \
                            --token
                        BRIDGE_RUN_ID="$BUILD_TAG" rebar bridge run
                    '''
                }
            }
        }
    }

    post {
        always {
            archiveArtifacts(
                artifacts: '.tickets-tracker/.bridge_state/last-pass.json',
                allowEmptyArchive: true
            )
        }
        failure {
            echo "Reconcile Bridge failed: ${env.BUILD_URL}"
        }
    }
}
