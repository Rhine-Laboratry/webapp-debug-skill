<?php
echo $this->Html->link('CSV', ['action' => 'exportCsv']);
echo $this->Form->postLink('Delete', ['action' => 'delete', 1]);
