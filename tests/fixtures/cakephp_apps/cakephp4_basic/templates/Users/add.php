<?php
echo $this->Form->create($user);
echo $this->Form->control('name');
echo $this->Form->button('Save');
echo $this->Form->end();
