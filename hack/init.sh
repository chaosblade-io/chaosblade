#!/usr/bin/env bash

function git_find() {
    # Similar to find but faster and easier to understand.  We want to include
    # modified and untracked files because this might be running against code
    # which is not tracked by git yet.
    git ls-files -cmo --exclude-standard \
        ':!:vendor/*'        `# catches vendor/...` \
        ':!:*/vendor/*'      `# catches any subdir/vendor/...` \
        ':!:third_party/*'   `# catches third_party/...` \
        ':!:*/third_party/*' `# catches third_party/...` \
        ':!:*/testdata/*'    `# catches any subdir/testdata/...` \
        ':(glob)**/*.go' \
        "$@"
}