<?php
echo $this->Html->link('Add', ['action' => 'add']);
foreach ($users as $user) {
    echo h($user->name);
}
